"""Backend for the Automation Lab Marketplace Desktop plugin."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.routing import APIRoute
from hermes_constants import get_hermes_home


def _instance_id() -> str:
    # ponytail: persistent per-destination UUID, not a process identity or path alone.
    with _file_lock("identity", 30):
        path = _data_dir() / "instance.json"
        if not path.exists():
            _atomic_json(path, {"id": uuid.uuid4().hex})
        value = _bounded_json(path, 256)
        if not isinstance(value, dict) or not re.fullmatch(r"[0-9a-f]{32}", str(value.get("id", ""))):
            raise RuntimeError("Invalid marketplace instance identity")
        return value["id"]

def _target(profile: str) -> dict[str, Any]:
    import hashlib
    return {"profile": profile, "id": hashlib.sha256(f"{_instance_id()}:{_home()}".encode()).hexdigest(), "protocol": 1}


class ScopedRoute(APIRoute):
    """One await-safe authorization/context boundary for every plugin HTTP route."""
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def scoped(request: Request):
            try:
                from hermes_cli.web_server import _require_token
                from hermes_cli.web_server_profiles import _config_profile_scope
            except ImportError as exc:
                raise HTTPException(503, "This backend lacks supported profile isolation; marketplace unavailable") from exc
            _require_token(request)
            # Legacy HTTP policy remains closed on OAuth. The native CLI adapter
            # is separately admitted by Hermes' machine-admin transport.
            if getattr(request.app.state, "auth_required", False):
                raise HTTPException(403, "Marketplace unavailable on this shared authenticated backend: "
                                    "Hermes does not expose profile authorization to plugins")
            profiles = request.query_params.getlist("profile")
            if len(profiles) != 1 or not profiles[0] or profiles[0] != profiles[0].strip() or profiles[0].lower() in {"all", "current"}:
                raise HTTPException(400, "An explicit, existing profile is required")
            profile = profiles[0]
            # Native validation/existence checks precede the context override.
            # ContextVars propagate into asyncio.to_thread; never change os.environ.
            with _config_profile_scope(profile):
                from hermes_cli.plugins_cmd import _get_enabled_set, _get_disabled_set
                # Core's runtime gate checks the launch home, not the query
                # profile. Respect this destination's own activation as well.
                if (PLUGIN_ID not in _get_enabled_set() or PLUGIN_ID in _get_disabled_set()
                        or not (_home() / "plugins" / PLUGIN_ID / "dashboard" / "plugin_api.py").is_file()):
                    raise HTTPException(404, "Plugin not found")
                target = _target(profile)
                if request.method != "GET":
                    try:
                        body = await request.json()
                    except (ValueError, UnicodeDecodeError) as exc:
                        raise HTTPException(400, "Confirmed destination is required") from exc
                    expected = body.get("expected_target") if isinstance(body, dict) else None
                else:
                    expected = request.query_params.get("expected_target")
                if request.url.path.rsplit("/", 1)[-1] != "state" and expected != target["id"]:
                    raise HTTPException(409, "Installation destination changed or is unconfirmed; reopen the marketplace")
                response = await handler(request)
                if 200 <= response.status_code < 300:
                    payload = json.loads(bytes(response.body))
                    payload["target"] = target
                    response.body = json.dumps(payload).encode()
                    response.headers["content-length"] = str(len(response.body))
                return response
        return scoped


router = APIRouter(route_class=ScopedRoute)

PLUGIN_ID = "automation-lab-marketplace"
REPO = "Carl-Taylor-Automation-Lab/automation-lab-plugin-marketplace"
REPO_URL = f"https://github.com/{REPO}.git"
REPOSITORY_ID = 1259339326
MARKETPLACES = {
    "lab": {"repo": REPO, "id": REPOSITORY_ID, "label": "Automation Lab"},
    "premium": {"repo": "Carl-Taylor-Automation-Lab/aa-premium-plugin-marketplace",
                "id": 1325136519, "label": "Premium"},
}
# Public identifier, not a secret. Environment override is for development only.
GITHUB_CLIENT_ID = os.environ.get("AUTOMATION_LAB_GITHUB_CLIENT_ID", "Iv23liapu786M8QbKBjz").strip()
_PACKAGE_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_FLOWS: dict[str, dict[str, Any]] = {}
_FLOWS_THREAD_LOCK = threading.Lock()


@contextmanager
def _flows_lock():
    # ponytail: short global sections; network requests never hold this lock.
    with _FLOWS_THREAD_LOCK, _file_lock("flows", 30):
        path = _data_dir() / "github-flows.json"
        flows = _bounded_json(path, 128_000) if path.exists() else {}
        if not isinstance(flows, dict):
            raise RuntimeError("Invalid GitHub flow state")
        _FLOWS.clear()
        _FLOWS.update(flows)
        try:
            yield
        finally:
            _atomic_json(path, _FLOWS)


def _home() -> Path:
    return Path(get_hermes_home()).expanduser().resolve()


def _secure_path(path: Path, *, directory: bool = False) -> None:
    if os.name != "nt":
        path.chmod(0o700 if directory else 0o600)
        return
    import ctypes
    from ctypes import wintypes

    # Replace the entire protected DACL, not just the current user's ACE.
    result = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                            capture_output=True, text=True, timeout=15,
                            creationflags=subprocess.CREATE_NO_WINDOW, check=True)
    sid = re.search(r"S-1-5-[0-9-]+", result.stdout)
    if not sid:
        raise RuntimeError("Could not identify Windows credential owner")
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    convert.restype = wintypes.BOOL
    apply = advapi.SetFileSecurityW
    apply.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    apply.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    descriptor = ctypes.c_void_p()
    inheritance = "OICI" if directory else ""
    if not convert(f"D:P(A;{inheritance};FA;;;{sid.group()})", 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not apply(str(path), 0x80000004, descriptor):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.LocalFree(descriptor)


def _data_dir() -> Path:
    home = _home()
    path = home / "plugin-data" / PLUGIN_ID
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.resolve().relative_to(home)
    except ValueError as exc:
        raise RuntimeError("Marketplace data directory escapes HERMES_HOME") from exc
    if path.is_symlink():
        raise RuntimeError("Marketplace data directory must not be a symlink")
    _secure_path(path, directory=True)
    return path


def _auth_path() -> Path:
    return _data_dir() / "github-auth.json"


def _read_json(path: Path, default: Any) -> Any:
    try:
        return _bounded_json(path, 2_000_000)
    except (FileNotFoundError, OSError, ValueError, TypeError, RuntimeError):
        return default


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _secure_path(Path(tmp_name))
        os.replace(tmp_name, path)
        _secure_path(path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _clear_auth() -> None:
    try:
        _auth_path().unlink()
    except FileNotFoundError:
        pass


@contextmanager
def _file_lock(name: str, timeout: int):
    path = _data_dir() / f".{name}.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise RuntimeError("Marketplace lock must be a regular file")
    handle = os.fdopen(fd, "r+b")
    if os.fstat(handle.fileno()).st_size == 0:
        handle.write(b"\0")
        handle.flush()
    deadline = time.monotonic() + timeout
    locked = False
    try:
        while not locked:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"Timed out waiting for the marketplace {name} lock")
                time.sleep(0.1)
        yield
    finally:
        if locked:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _request_json(
    url: str,
    *,
    token: str | None = None,
    form: dict[str, str] | None = None,
    timeout: int = 20,
) -> Any:
    headers = {
        "Accept": "application/json",
        "User-Agent": "automation-lab-hermes-marketplace",
    }
    data = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-GitHub-Api-Version"] = "2026-03-10"
    if form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read(1_000_001)
            if len(payload) > 1_000_000:
                raise RuntimeError("GitHub response exceeded 1 MB")
            return json.loads(payload.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise PermissionError("GitHub authorization is invalid or revoked") from exc
        raise RuntimeError(f"GitHub returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"GitHub request failed: {exc}") from exc


def _verify_access(token: str) -> dict[str, Any]:
    user = _request_json("https://api.github.com/user", token=token)
    if not isinstance(user, dict) or not isinstance(user.get("login"), str):
        raise PermissionError("GitHub returned an invalid user identity")
    installations = _request_json("https://api.github.com/user/installations?per_page=100", token=token)
    if (not isinstance(installations, dict)
            or not isinstance(installations.get("installations"), list)
            or installations.get("total_count", len(installations["installations"])) > 100):
        raise RuntimeError("GitHub returned an invalid installation list")
    allowed = {market["id"] for market in MARKETPLACES.values()}
    accessible = set()
    for installation in installations["installations"]:
        if not isinstance(installation, dict) or not isinstance(installation.get("id"), int):
            raise RuntimeError("GitHub returned an invalid installation")
        repositories = _request_json(
            f"https://api.github.com/user/installations/{installation['id']}/repositories?per_page=100",
            token=token)
        if not isinstance(repositories, dict) or not isinstance(repositories.get("repositories"), list):
            raise RuntimeError("GitHub returned an invalid repository list")
        ids = {item.get("id") for item in repositories["repositories"] if isinstance(item, dict)}
        if repositories.get("total_count") != len(ids) or not ids <= allowed:
            raise PermissionError("GitHub App access exceeds the two marketplace repositories")
        permissions = installation.get("permissions", {})
        if not isinstance(permissions, dict) or any(value != "read" for value in permissions.values()):
            raise PermissionError("GitHub App repository permissions must be read-only")
        if ids and permissions.get("contents") != "read":
            raise RuntimeError("GitHub App installation requires Contents: read-only. "
                               "Its owner must enable that permission and approve it on the installation.")
        accessible.update(ids)
    return {**user, "marketplaces": [key for key, value in MARKETPLACES.items() if value["id"] in accessible]}

def _save_auth(payload: dict[str, Any], username: str) -> None:
    now = int(time.time())
    value = {
        "access_token": payload["access_token"],
        "username": username,
        "created_at": now,
    }
    if payload.get("expires_in"):
        value["expires_at"] = now + int(payload["expires_in"])
    if payload.get("refresh_token"):
        value["refresh_token"] = payload["refresh_token"]
        value["refresh_expires_at"] = now + int(payload.get("refresh_token_expires_in", 0))
    _atomic_json(_auth_path(), value)


def _store_verified_auth(payload: dict[str, Any], username: str, flow_id: str) -> None:
    with _file_lock("auth", 30), _flows_lock():
        flow = _FLOWS.get(flow_id)
        if not flow or flow["home"] != str(_home()):
            raise RuntimeError("GitHub connection was cancelled")
        _save_auth(payload, username)


def _clear_rejected_auth(token: str) -> None:
    with _file_lock("auth", 30):
        auth = _read_json(_auth_path(), {})
        if isinstance(auth, dict) and auth.get("access_token") == token:
            _clear_auth()


def _disconnect() -> None:
    with _file_lock("auth", 30), _flows_lock():
        for key in [key for key, value in _FLOWS.items() if value["home"] == str(_home())]:
            _FLOWS.pop(key, None)
        _clear_auth()


def _access_token() -> str:
    with _file_lock("auth", 30):
        auth = _read_json(_auth_path(), {})
        token = auth.get("access_token") if isinstance(auth, dict) else None
        if not isinstance(token, str) or len(token) < 20:
            raise HTTPException(401, "Connect GitHub first")
        expires_at = int(auth.get("expires_at") or 0)
        if not expires_at or expires_at > time.time() + 300:
            return token
        refresh = auth.get("refresh_token")
        if not isinstance(refresh, str) or not refresh or int(auth.get("refresh_expires_at") or 0) <= time.time():
            _clear_auth()
            raise HTTPException(401, "GitHub connection expired; connect again")
        if not GITHUB_CLIENT_ID:
            raise HTTPException(503, "GitHub App is not configured")
        try:
            payload = _request_json(
                "https://github.com/login/oauth/access_token",
                form={
                    "client_id": GITHUB_CLIENT_ID,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh,
                },
            )
            if not isinstance(payload, dict) or payload.get("error") or not payload.get("access_token"):
                raise RuntimeError(
                    payload.get("error_description") or payload.get("error") or "refresh failed"
                    if isinstance(payload, dict)
                    else "invalid refresh response"
                )
            _save_auth(payload, str(auth.get("username") or ""))
            return str(payload["access_token"])
        except PermissionError as exc:
            _clear_auth()
            raise HTTPException(401, "GitHub connection was revoked; connect again") from exc
        except RuntimeError as exc:
            raise HTTPException(502, f"GitHub connection refresh failed; retry: {exc}") from exc


@contextmanager
def _askpass(token: str):
    directory = Path(tempfile.mkdtemp(prefix="automation-lab-askpass-"))
    try:
        if os.name == "nt":
            helper = directory / "askpass.cmd"
            helper.write_text(
                "@echo off\r\n"
                "echo %~1 | findstr /I username >nul\r\n"
                "if %errorlevel%==0 (echo x-access-token) else (echo %AUTOMATION_LAB_GITHUB_TOKEN%)\r\n",
                encoding="utf-8",
            )
        else:
            helper = directory / "askpass.sh"
            helper.write_text(
                "#!/bin/sh\ncase \"$1\" in *sername*) printf '%s\\n' x-access-token ;; "
                "*) printf '%s\\n' \"$AUTOMATION_LAB_GITHUB_TOKEN\" ;; esac\n",
                encoding="utf-8",
            )
            helper.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        # Git accepts config, rewrites, tracing, alternate object stores and
        # executable helpers through many GIT_* variables. Inherit none of them.
        env = {k: v for k, v in os.environ.items()
               if not k.upper().startswith(("GIT_", "GCM_"))}
        env.update(
            {
                "AUTOMATION_LAB_GITHUB_TOKEN": token,
                "GIT_ASKPASS": str(helper),
                "GIT_TERMINAL_PROMPT": "0",
                "GCM_INTERACTIVE": "Never",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "credential.helper",
                "GIT_CONFIG_VALUE_0": "",
                "GIT_ALLOW_PROTOCOL": "https",
                "GIT_CEILING_DIRECTORIES": str(directory.parent),
                "GIT_TEMPLATE_DIR": str(directory / "empty-template"),
                "HERMES_HOME": str(_home()),
            }
        )
        (directory / "empty-template").mkdir()
        yield env
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@contextmanager
def _process_job():
    if os.name != "nt":
        yield None
        return
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                    ("flags", wintypes.DWORD), ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t),
                    ("active_limit", wintypes.DWORD), ("affinity", ctypes.c_size_t),
                    ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]
    class Limits(ctypes.Structure):
        _fields_ = [("basic", Basic), ("io", ctypes.c_ulonglong * 6),
                    ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                    ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        limits = Limits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, no breakaway.
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        yield (job, kernel)
    finally:
        kernel.CloseHandle(job)


def _resume_in_job(process, job):
    import ctypes
    from ctypes import wintypes
    handle, kernel = job
    # CPython retains the native process handle; spawn suspended so descendants
    # cannot escape before assignment. Windows 8+ supports nested job objects.
    if not kernel.AssignProcessToJobObject(handle, int(process._handle)):
        raise ctypes.WinError(ctypes.get_last_error())
    resume = ctypes.WinDLL("ntdll").NtResumeProcess
    resume.argtypes = [wintypes.HANDLE]
    resume.restype = wintypes.LONG
    if resume(int(process._handle)) != 0:
        raise RuntimeError("Could not resume the isolated Windows installer")


def _run(
    args: list[str],
    *,
    env: dict[str, str],
    timeout: int = 180,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    creationflags = 0
    popen_kwargs: dict[str, Any] = {}
    if os.name == "nt":
        creationflags = 0x4 | getattr(subprocess, "CREATE_NO_WINDOW", 0)  # CREATE_SUSPENDED
    else:
        popen_kwargs["start_new_session"] = True
    with _process_job() as job:
        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            env=env,
            cwd=str(cwd) if cwd else tempfile.gettempdir(),
            creationflags=creationflags,
            **popen_kwargs,
        )
        try:
            if job:
                _resume_in_job(process, job)
        except Exception:
            process.kill()
            process.wait(timeout=5)
            raise
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            if job:
                job[1].TerminateJobObject(job[0], 1)
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                if process.stdout:
                    process.stdout.close()
                if process.stderr:
                    process.stderr.close()
            raise
        return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)


def _bounded_json(path: Path, limit: int) -> Any:
    # Read the descriptor we validated, with a hard cap even if the file grows.
    fd = None
    parent = None
    windows_parents = []
    try:
        if os.name == "nt":
            import ctypes
            import msvcrt
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            create = kernel.CreateFileW
            create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
            create.restype = wintypes.HANDLE
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            # Pin every ancestor against rename, rejecting junctions/reparse points.
            for ancestor in reversed(path.absolute().parents):
                handle = create(str(ancestor), 0x80000000, 3, None, 3, 0x02200000, None)
                if handle == ctypes.c_void_p(-1).value:
                    raise ctypes.WinError(ctypes.get_last_error())
                windows_parents.append(handle)
                class AttributeTag(ctypes.Structure):
                    _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]
                info_tag = AttributeTag()
                query = kernel.GetFileInformationByHandleEx
                query.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
                if not query(handle, 9, ctypes.byref(info_tag), ctypes.sizeof(info_tag)):
                    raise ctypes.WinError(ctypes.get_last_error())
                if info_tag.attributes & 0x400:
                    raise RuntimeError("Marketplace path contains a reparse point")
            handle = create(str(path), 0x80000000, 1, None, 3, 0x00200000, None)
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
            except OSError:
                kernel.CloseHandle(handle)
                raise
        else:
            absolute = path.absolute()
            parent = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY)
            for part in absolute.parts[1:-1]:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                os.close(parent)
                parent = next_fd
            fd = os.open(absolute.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_size > limit
                or getattr(info, "st_file_attributes", 0) & 0x400):
            raise RuntimeError(f"Marketplace file is unsafe or too large: {path.name}")
        with os.fdopen(fd, "rb") as stream:
            fd = None
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise RuntimeError(f"Marketplace file is too large: {path.name}")
        return json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Marketplace file is invalid: {path.name}") from exc
    finally:
        if fd is not None:
            os.close(fd)
        if parent is not None:
            os.close(parent)
        if windows_parents:
            for handle in reversed(windows_parents):
                kernel.CloseHandle(handle)


def _package_path(root: Path, source: str, name: str) -> Path:
    relative = Path(source)
    if relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError(f"Marketplace source for {name} is invalid")
    candidate = root
    for part in relative.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise RuntimeError(f"Marketplace source for {name} contains a symlink")
    try:
        candidate.resolve().relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"Marketplace source for {name} escapes the repository") from exc
    if not candidate.is_dir():
        raise RuntimeError(f"Marketplace package for {name} is missing")
    return candidate


def _clone_catalog(token: str) -> list[dict[str, Any]]:
    user = _verify_access(token)
    entries = []
    for market in user["marketplaces"]:
        entries.extend(_clone_repository(token, market))
    return entries


def _clone_repository(token: str, market: str) -> list[dict[str, Any]]:
    repository = MARKETPLACES[market]
    metadata = _request_json(f"https://api.github.com/repos/{repository['repo']}", token=token)
    if not isinstance(metadata, dict) or metadata.get("id") != repository["id"]:
        raise PermissionError("GitHub repository identity does not match the marketplace")
    checkout = Path(tempfile.mkdtemp(prefix="automation-lab-catalog-"))
    try:
        with _askpass(token) as env:
            result = _run(["git", "clone", "--depth", "1", f"https://github.com/{repository['repo']}.git", str(checkout)], env=env, timeout=90)
            if result.returncode:
                raise RuntimeError((result.stderr or result.stdout or "Git clone failed")[-2000:])
            head = _run(["git", "rev-parse", "HEAD"], env=env, timeout=15, cwd=checkout)
            trees = _run(["git", "ls-tree", "-r", "-d", "HEAD", "--", "plugins"], env=env, timeout=15, cwd=checkout)
        if trees.returncode:
            raise RuntimeError("Could not resolve package tree identities")
        tree_ids = {}
        for line in trees.stdout.splitlines():
            info, path = line.split("\t", 1)
            tree_ids[path] = info.split()[2]
        revision = head.stdout.strip().lower()
        if head.returncode or not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise RuntimeError("Could not resolve the marketplace revision")
        root = checkout.resolve()
        marketplace = _bounded_json(root / ".claude-plugin" / "marketplace.json", 2_000_000)
        if not isinstance(marketplace, dict) or not isinstance(marketplace.get("plugins"), list):
            raise RuntimeError("Marketplace manifest must contain a plugin list")
        if not 1 <= len(marketplace["plugins"]) <= 200:
            raise RuntimeError("Marketplace plugin count is invalid")
        entries = []
        seen: set[str] = set()
        for raw in marketplace["plugins"]:
            if not isinstance(raw, dict):
                raise RuntimeError("Marketplace contains a non-object plugin entry")
            name = raw.get("name")
            source = raw.get("source")
            if not isinstance(name, str) or not _PACKAGE_RE.fullmatch(name) or name in seen:
                raise RuntimeError("Marketplace contains an invalid or duplicate plugin name")
            if not isinstance(source, str) or not source.startswith("./plugins/"):
                raise RuntimeError(f"Marketplace source for {name} is invalid")
            source = source[2:]
            package = _package_path(root, source, name)
            tree_sha = tree_ids.get(source)
            if not isinstance(tree_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", tree_sha):
                raise RuntimeError("Package tree identity is missing")
            manifest_path = package / "plugin.json"
            if not manifest_path.exists():
                manifest_path = package / ".claude-plugin" / "plugin.json"
            manifest = _bounded_json(manifest_path, 128_000)
            if not isinstance(manifest, dict) or manifest.get("name") != name:
                raise RuntimeError(f"Manifest name mismatch for {name}")
            version = manifest.get("version")
            display_name = raw.get("displayName") or name
            description = raw.get("description") or manifest.get("description") or ""
            if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
                raise RuntimeError(f"Manifest version for {name} is invalid")
            if not isinstance(display_name, str) or not 1 <= len(display_name) <= 200:
                raise RuntimeError(f"Display name for {name} is invalid")
            if not isinstance(description, str) or len(description) > 4000:
                raise RuntimeError(f"Description for {name} is invalid")
            skill_count = 0
            skills = package / "skills"
            if skills.exists():
                if skills.is_symlink() or not skills.is_dir():
                    raise RuntimeError(f"Skills directory for {name} is unsafe")
                for skill in skills.glob("*/SKILL.md"):
                    info = skill.lstat()
                    if skill.is_symlink() or not stat.S_ISREG(info.st_mode):
                        raise RuntimeError(f"Skill file for {name} is unsafe")
                    skill_count += 1
                    if skill_count > 500:
                        raise RuntimeError(f"Plugin {name} contains too many skills")
            mcp_count = 0
            mcp = {}
            mcp_path = package / "mcp.json"
            if not mcp_path.exists():
                mcp_path = package / ".mcp.json"
            if mcp_path.exists() or mcp_path.is_symlink():
                mcp = _bounded_json(mcp_path, 256_000)
                servers = mcp.get("mcpServers") if isinstance(mcp, dict) else None
                if not isinstance(servers, dict) or len(servers) > 100:
                    raise RuntimeError(f"MCP manifest for {name} is invalid")
                mcp_count = len(servers)
            entries.append(
                {
                    "name": name,
                    "marketplace": market,
                    "marketplace_label": repository["label"],
                    "display_name": display_name,
                    "capabilities": manifest.get("capabilities", []),
                    "mcp": mcp,
                    "description": description,
                    "version": version,
                    "source": source,
                    "revision": revision,
                    "tree_sha": tree_sha,
                    "skills": skill_count,
                    "connectors": mcp_count,
                }
            )
            seen.add(name)
        return entries
    finally:
        shutil.rmtree(checkout, ignore_errors=True)


def _metadata() -> dict[str, Any]:
    value = _read_json(_home() / "plugins" / ".install-metadata.json", {})
    return value if isinstance(value, dict) else {}


def _version(version: str) -> tuple[int, int, int]:
    match = _VERSION_RE.fullmatch(version)
    if not match:
        raise RuntimeError(f"Invalid semantic version: {version}")
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def _enabled_plugins() -> list[str]:
    from hermes_cli.config import load_config
    return load_config().get("plugins", {}).get("enabled", [])


def _installed(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    metadata = _metadata()
    plugin_root = _home() / "plugins"
    result = []
    for entry in entries:
        row = {key: value for key, value in entry.items() if key not in {"mcp", "capabilities"}}
        manifest = _read_json(plugin_root / entry["name"] / "plugin.json", {})
        installed_version = manifest.get("version") if isinstance(manifest, dict) else None
        record = metadata.get(entry["name"], {}) if isinstance(metadata.get(entry["name"]), dict) else {}
        repo = MARKETPLACES[entry.get("marketplace", "lab")]["repo"]
        expected_source = f"https://github.com/{repo}.git#{entry['source']}"
        source_conflict = bool(installed_version and record.get("source") != expected_source)
        downgrade_blocked = bool(
            installed_version
            and _VERSION_RE.fullmatch(str(installed_version))
            and _version(str(installed_version)) > _version(entry["version"])
        )
        stamp = _read_json(plugin_root / entry["name"] / ".marketplace-tree.json", {})
        unchanged = (isinstance(stamp, dict) and entry.get("tree_sha")
                     and stamp.get("tree_sha") == entry["tree_sha"])
        row.update(
            {
                "installed": bool(installed_version),
                "enabled": entry["name"] in _enabled_plugins(),
                "installed_version": installed_version,
                "source_conflict": source_conflict,
                "downgrade_blocked": downgrade_blocked,
                "update_available": bool(
                    installed_version
                    and not source_conflict
                    and not downgrade_blocked
                    and not unchanged
                    and record.get("revision") != entry["revision"]
                ),
            }
        )
        result.append(row)
    return result


def _connection_state() -> dict[str, Any]:
    with _file_lock("auth", 30):
        auth = _read_json(_auth_path(), {})
        if not isinstance(auth, dict):
            auth = {}
        token = auth.get("access_token")
        expires_at = int(auth.get("expires_at") or 0)
        refresh_until = int(auth.get("refresh_expires_at") or 0)
        connected = isinstance(token, str) and len(token) >= 20 and (
            not expires_at or expires_at > time.time() or refresh_until > time.time()
        )
        if token and not connected:
            _clear_auth()
    return {
        "auto_updates": _read_json(_data_dir() / "settings.json", {}).get("auto_updates") is True,
        "configured": bool(GITHUB_CLIENT_ID),
        "connected": connected,
        "username": auth.get("username") if connected else None,
    }


def _catalog_response() -> dict[str, Any]:
    token = _access_token()
    try:
        return {"plugins": _installed(_clone_catalog(token))}
    except PermissionError as exc:
        _clear_rejected_auth(token)
        raise HTTPException(401, "GitHub access expired or has the wrong repository scope; reconnect") from exc
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(504, "Marketplace Git operation timed out") from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc


def _install_plugin(body: dict[str, Any]) -> dict[str, Any]:
    name = body.get("name")
    if not isinstance(name, str) or not _PACKAGE_RE.fullmatch(name):
        raise HTTPException(400, "Invalid plugin name")
    token = _access_token()
    try:
        market = body.get("marketplace", "lab")
        if not isinstance(market, str) or market not in MARKETPLACES:
            raise HTTPException(400, "Unknown marketplace")
        entries = {entry["name"]: entry for entry in _clone_catalog(token)
                   if entry.get("marketplace", "lab") == market}
    except PermissionError as exc:
        _clear_rejected_auth(token)
        raise HTTPException(401, "GitHub access expired or has the wrong repository scope; reconnect") from exc
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(504, "Marketplace Git operation timed out") from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    entry = entries.get(name)
    if not entry:
        raise HTTPException(404, "Plugin is not in the Automation Lab marketplace")
    enable = body.get("enable", True) is not False
    reviewed = body.get("reviewed_revision") == entry["revision"] and body.get("automatic") is not True
    with _file_lock("install", 300):
        plugin_dir = _home() / "plugins" / name
        existing = plugin_dir.exists()
        original_record = _metadata().get(name)
        original_inode = plugin_dir.stat().st_ino if existing else None
        if plugin_dir.is_symlink():
            raise HTTPException(409, "Installed plugin must not be a symlink")
        repo = MARKETPLACES[market]["repo"]
        expected_source = f"https://github.com/{repo}.git#{entry['source']}"
        if existing:
            record = _metadata().get(name, {})
            if not isinstance(record, dict) or record.get("source") != expected_source:
                raise HTTPException(409, f"{name} is installed from another source and will not be overwritten")
            manifest = _read_json(plugin_dir / "plugin.json", {})
            current = manifest.get("version") if isinstance(manifest, dict) else None
            if not isinstance(current, str) or not _VERSION_RE.fullmatch(current):
                raise HTTPException(409, f"The installed {name} version is invalid")
            if _version(current) > _version(entry["version"]):
                raise HTTPException(409, f"{name} {current} is newer than marketplace version {entry['version']}")
            if body.get("automatic") is True:
                old_mcp = _read_json(plugin_dir / "mcp.json", {})
                if (manifest.get("capabilities", []) != entry.get("capabilities", [])
                        or old_mcp != entry.get("mcp", {})):
                    raise HTTPException(409, "Update paused: capabilities or connector configuration changed; review manually")
            stamp = _read_json(plugin_dir / ".marketplace-tree.json", {})
            unchanged = (isinstance(stamp, dict) and entry.get("tree_sha")
                         and stamp.get("tree_sha") == entry["tree_sha"])
            if unchanged or record.get("revision") == entry["revision"]:
                return {"ok": True, "name": name, "version": current, "restart_required": False}
        args = [
            sys.executable,
            str(Path(__file__).with_name("install_package.py")),
            f"{repo}/{entry['source']}",
            "--name", name,
            "--tree", entry.get("tree_sha", ""),
            "--ref",
            entry["revision"],
        ]
        if existing or reviewed:
            args.append("--force")
        # Updates never silently re-enable a plugin disabled by the member.
        args.append("--enable" if enable and not existing else "--no-enable")
        try:
            with _askpass(token) as env:
                scan_home = Path(tempfile.mkdtemp(prefix="automation-lab-scan-")).resolve()
                try:
                    scan_args = [part for part in args if part != "--force" or reviewed]
                    scan_args.extend(["--scan-report", str(scan_home / "scan-result.json")])
                    scan_env = dict(env, HERMES_HOME=str(scan_home))
                    scan = _run(scan_args, env=scan_env, timeout=240)
                    scan_report = _read_json(scan_home / "scan-result.json", {})
                finally:
                    shutil.rmtree(scan_home, ignore_errors=True)
                scan_output = ((scan.stdout or "") + "\n" + (scan.stderr or "")).replace(token, "[REDACTED]")[-128000:]
                if scan.returncode:
                    if (isinstance(scan_report, dict) and scan_report.get("verdict") == "caution"
                            and not reviewed and body.get("automatic") is not True):
                        return {"ok": False, "review_required": True, "name": name, "marketplace": market,
                                "revision": entry["revision"], "report": scan_output.strip()}
                    status = 409 if scan_report else 502
                    raise HTTPException(status, scan_output.strip() or "Plugin installation validation failed")
                if (_metadata().get(name) != original_record
                        or (plugin_dir.stat().st_ino if plugin_dir.exists() else None) != original_inode):
                    raise HTTPException(409, "Plugin changed in another surface during validation; retry")
                result = _run(args, env=env, timeout=240)
        except subprocess.TimeoutExpired as exc:
            raise HTTPException(504, "Plugin installation timed out") from exc
        output = ((result.stdout or "") + "\n" + (result.stderr or "")).replace(token, "[REDACTED]")[-4000:]
        if result.returncode:
            raise HTTPException(502, output.strip() or "Plugin installation failed")
        record = _metadata().get(name, {})
        if not isinstance(record, dict) or record.get("source") != expected_source or record.get("revision") != entry["revision"]:
            raise HTTPException(500, "Hermes installed the plugin but its provenance did not match the requested revision")
    return {"ok": True, "name": name, "version": entry["version"], "restart_required": True}


@router.get("/state")
async def state():
    return await asyncio.to_thread(_connection_state)


@router.post("/auth/start")
async def auth_start():
    if not GITHUB_CLIENT_ID:
        raise HTTPException(503, "GitHub App is not configured")
    flow_id = uuid.uuid4().hex
    home = str(_home())
    with _flows_lock():
        now = time.time()
        for key in [key for key, value in _FLOWS.items() if value["expires_at"] <= now]:
            _FLOWS.pop(key, None)
        if sum(value["home"] == home for value in _FLOWS.values()) >= 3:
            raise HTTPException(429, "Too many pending GitHub connections")
        _FLOWS[flow_id] = {"home": home, "expires_at": now + 60, "starting": True}
    try:
        payload = await asyncio.to_thread(
            _request_json,
            "https://github.com/login/device/code",
            form={"client_id": GITHUB_CLIENT_ID},
        )
        required = ("device_code", "user_code", "verification_uri", "expires_in", "interval")
        if not isinstance(payload, dict) or any(not payload.get(key) for key in required):
            raise RuntimeError("GitHub returned an invalid device-flow response")
        if (payload["verification_uri"] != "https://github.com/login/device"
                or not 1 <= int(payload["interval"]) <= 60
                or not 1 <= int(payload["expires_in"]) <= 1800):
            raise RuntimeError("GitHub returned unsafe device-flow parameters")
        flow = {
            "device_code": payload["device_code"],
            "expires_at": time.time() + int(payload["expires_in"]),
            "interval": int(payload["interval"]),
            "next_poll": 0.0,
            "home": home,
        }
        with _flows_lock():
            if flow_id not in _FLOWS:
                raise RuntimeError("GitHub connection was cancelled")
            _FLOWS[flow_id] = flow
    except asyncio.CancelledError:
        with _flows_lock():
            _FLOWS.pop(flow_id, None)
        raise
    except (RuntimeError, TypeError, ValueError) as exc:
        with _flows_lock():
            _FLOWS.pop(flow_id, None)
        raise HTTPException(502, str(exc)) from exc
    return {
        "flow_id": flow_id,
        "user_code": payload["user_code"],
        "verification_uri": payload["verification_uri"],
        "expires_in": int(payload["expires_in"]),
        "interval": int(payload["interval"]),
    }


@router.post("/auth/poll")
async def auth_poll(body: dict[str, Any]):
    flow_id = body.get("flow_id")
    if not isinstance(flow_id, str):
        raise HTTPException(400, "flow_id is required")
    with _flows_lock():
        flow = _FLOWS.get(flow_id)
        if not flow or flow["home"] != str(_home()):
            raise HTTPException(404, "Authorization flow not found")
        if flow["expires_at"] <= time.time():
            _FLOWS.pop(flow_id, None)
            raise HTTPException(410, "Authorization code expired")
        if flow.get("starting") or flow.get("polling_until", 0) > time.time():
            return {"status": "pending", "retry_after": 1}
        if flow["next_poll"] > time.time():
            return {"status": "pending", "retry_after": max(1, int(flow["next_poll"] - time.time()))}
        flow["next_poll"] = time.time() + flow["interval"]
        flow["polling_until"] = time.time() + 120
    try:
        payload = await asyncio.to_thread(
            _request_json,
            "https://github.com/login/oauth/access_token",
            form={
                "client_id": GITHUB_CLIENT_ID,
                "device_code": flow["device_code"],
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
        )
    except asyncio.CancelledError:
        with _flows_lock():
            _FLOWS.pop(flow_id, None)
        raise
    except (PermissionError, RuntimeError, ValueError, TypeError) as exc:
        with _flows_lock():
            _FLOWS.pop(flow_id, None)
        raise HTTPException(502, str(exc)) from exc
    error = payload.get("error") if isinstance(payload, dict) else "invalid_response"
    if error == "authorization_pending":
        with _flows_lock():
            if flow_id in _FLOWS:
                flow["polling_until"] = 0
                _FLOWS[flow_id] = flow
        return {"status": "pending", "retry_after": flow["interval"]}
    if error == "slow_down":
        with _flows_lock():
            flow["interval"] += 5
            flow["next_poll"] = time.time() + flow["interval"]
            if flow_id in _FLOWS:
                flow["polling_until"] = 0
                _FLOWS[flow_id] = flow
        return {"status": "pending", "retry_after": flow["interval"]}
    if error or not isinstance(payload, dict) or not payload.get("access_token"):
        with _flows_lock():
            _FLOWS.pop(flow_id, None)
        detail = payload.get("error_description") or error if isinstance(payload, dict) else error
        raise HTTPException(401, detail or "GitHub authorization failed")
    token = str(payload["access_token"])
    try:
        user = await asyncio.to_thread(_verify_access, token)
        await asyncio.to_thread(_store_verified_auth, payload, str(user["login"]), flow_id)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except (RuntimeError, OSError, ValueError, TypeError) as exc:
        raise HTTPException(502, str(exc)) from exc
    finally:
        with _flows_lock():
            _FLOWS.pop(flow_id, None)
    return {"status": "connected", "username": user["login"]}


@router.post("/logout")
async def logout():
    await asyncio.to_thread(_disconnect)
    return {"ok": True}


@router.get("/catalog")
async def catalog():
    return await asyncio.to_thread(_catalog_response)


@router.post("/install")
async def install(body: dict[str, Any]):
    return await asyncio.to_thread(_install_plugin, body)

@router.post("/settings")
async def settings(body: dict[str, Any]):
    if type(body.get("auto_updates")) is not bool:
        raise HTTPException(400, "auto_updates must be a boolean")
    def save():
        with _file_lock("auto", 30):
            _atomic_json(_data_dir() / "settings.json", {"auto_updates": body["auto_updates"]})
    await asyncio.to_thread(save)
    return {"ok": True, "auto_updates": body["auto_updates"]}


def _automatic_updates() -> dict[str, Any]:
    # ponytail: one pass per hour while the marketplace is open; no extra daemon.
    with _file_lock("auto", 1):
        path = _data_dir() / "settings.json"
        prefs = _read_json(path, {})
        if not prefs.get("auto_updates") or time.time() - prefs.get("last_check", 0) < 3600:
            return {"results": []}
        prefs["last_check"] = time.time()
        _atomic_json(path, prefs)
        rows = _catalog_response()["plugins"]
        for row in rows:
            if not row["update_available"]:
                continue
            try:
                result = _install_plugin({"name": row["name"], "marketplace": row["marketplace"],
                                          "enable": False, "automatic": True})
            except (HTTPException, RuntimeError) as exc:
                result = {"name": row["name"], "error": str(getattr(exc, "detail", exc))}
            # ponytail: at most one package per pass bounds a request; next pass
            # checks the remainder, while failed updates wait for manual review.
            prefs["last_check"] = time.time() - 3540
            if result.get("error"):
                prefs["auto_updates"] = False
                result["error"] += "; automatic updates paused"
            _atomic_json(path, prefs)
            return {"results": [result], "restart_required": result.get("restart_required", False)}
        return {"results": []}


@router.post("/updates/run")
async def automatic_updates():
    return await asyncio.to_thread(_automatic_updates)


def _uninstall_plugin(body: dict[str, Any]) -> dict[str, Any]:
    name = body.get("name")
    if not isinstance(name, str) or not _PACKAGE_RE.fullmatch(name):
        raise HTTPException(400, "Invalid plugin name")
    if name == PLUGIN_ID:
        raise HTTPException(409, "The running marketplace cannot remove itself")
    if body.get("confirm_name") != name:
        raise HTTPException(400, "Confirm the exact package name before uninstalling")
    with _file_lock("install", 300):
        root = _home() / "plugins"
        target = root / name
        if root.is_symlink() or target.is_symlink() or not target.is_dir():
            raise HTTPException(409, "Installed package path is missing or unsafe")
        record = _metadata().get(name, {})
        source = record.get("source", "") if isinstance(record, dict) else ""
        if not isinstance(source, str) or not any(source.startswith(f"https://github.com/{m['repo']}.git#plugins/") for m in MARKETPLACES.values()):
            raise HTTPException(409, "Only packages installed from these marketplaces can be uninstalled")
        market = body.get("marketplace", "lab")
        if not isinstance(market, str) or market not in MARKETPLACES:
            raise HTTPException(400, "Unknown marketplace")
        entry = next((entry for entry in _catalog_response()["plugins"]
                      if entry["name"] == name and entry["marketplace"] == market), None)
        if not entry or source != f"https://github.com/{MARKETPLACES[market]['repo']}.git#{entry['source']}":
            raise HTTPException(409, "Installed source does not match the current catalogue entry")
        identity = target.stat()
        from hermes_cli.plugins_cmd import _remove_plugin_core
        # Native CLI removal preserves enablement preferences. Do not pre-disable:
        # failures must leave those flags intact without rolling back other edits.
        # Pin the validated object before deletion. Native CLI writers do not
        # share our lock: a replaced object is restored, never recursively deleted.
        staging = Path(tempfile.mkdtemp(prefix=".lab-uninstall-", dir=root))
        pinned = staging / name
        try:
            os.replace(target, pinned)
            observed = pinned.lstat()
            if (not stat.S_ISDIR(observed.st_mode)
                    or (observed.st_dev, observed.st_ino) != (identity.st_dev, identity.st_ino)
                    or _metadata().get(name) != record or target.exists()):
                raise HTTPException(409, "Package changed during uninstall; nothing was deleted")
            # Native removal keeps the same package name, and scopes metadata
            # through the request home. Separate user files are never touched.
            _remove_plugin_core(pinned)
        except Exception as exc:
            if pinned.exists() or pinned.is_symlink():
                if target.exists() or target.is_symlink():
                    raise HTTPException(409, f"Concurrent package change; preserved recovery copy at {pinned}") from exc
                os.rename(pinned, target)
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(500, "Uninstall failed; refresh package state before retrying") from exc
        finally:
            # Never recursively clean a recovery folder.
            if not any(staging.iterdir()):
                staging.rmdir()
        if target.exists() or name in _metadata():
            raise HTTPException(409, "Concurrent package change; refresh installed state before retrying")
    return {"ok": True, "name": name, "restart_required": True}


@router.post("/uninstall")
async def uninstall(body: dict[str, Any]):
    return await asyncio.to_thread(_uninstall_plugin, body)


@router.post("/enabled")
async def enabled(body: dict[str, Any]):
    name = body.get("name")
    if not isinstance(name, str) or not _PACKAGE_RE.fullmatch(name) or type(body.get("enabled")) is not bool:
        raise HTTPException(400, "Invalid plugin or enabled flag")
    def apply():
        with _file_lock("install", 300):
            record = _metadata().get(name, {})
            if not isinstance(record, dict) or record.get("source") not in {
                f"https://github.com/{m['repo']}.git#plugins/{name}" for m in MARKETPLACES.values()
            } or not (_home() / "plugins" / name / "plugin.json").is_file():
                raise HTTPException(409, "Plugin is not installed from these marketplaces")
            from hermes_cli.plugins_cmd import _set_plugin_enabled
            _set_plugin_enabled(name, enable=body["enabled"])
            if (name in _enabled_plugins()) != body["enabled"]:
                raise HTTPException(500, "Plugin enabled state did not persist")
        return {"ok": True, "restart_required": True}
    return await asyncio.to_thread(apply)
