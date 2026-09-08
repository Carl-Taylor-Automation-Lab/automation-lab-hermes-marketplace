"""Automation Lab's native machine-administrator CLI adapter."""


def _parser(parser):
    parser.add_argument("operation", choices=("state", "auth/start", "auth/poll", "logout", "catalog",
                                             "install", "settings", "updates/run", "uninstall", "enabled"))
    parser.add_argument("--request", default="{}", help="Public operation parameters only; never credentials")
    parser.add_argument("--profile-name", required=True)


def _run(args):
    import asyncio
    import contextlib
    import io
    import json
    from pathlib import Path
    from hermes_cli.profiles import resolve_profile_env
    from .dashboard import plugin_api as api

    # Native cli.exec is an authenticated machine-admin surface, not messaging.
    # No arbitrary dispatch, release source, credential input, or HTTP bypass flag.
    try:
        if len(args.request) > 8192:
            raise api.HTTPException(400, "Request too large")
        body = json.loads(args.request)
        allowed = {"expected_target", "flow_id", "name", "marketplace", "enable", "enabled",
                   "reviewed_revision", "automatic", "auto_updates", "confirm_name"}
        if not isinstance(body, dict) or set(body) - allowed:
            raise api.HTTPException(400, "Unsupported operation parameters")
        profile = args.profile_name
        if Path(resolve_profile_env(profile)).resolve() != api._home():
            raise api.HTTPException(409, "Explicit CLI profile does not match destination")
        target = api._target(profile)
        if args.operation != "state" and body.get("expected_target") != target["id"]:
            raise api.HTTPException(409, "Installation destination changed or is unconfirmed; reopen the marketplace")
        handlers = {"state": api.state, "auth/start": api.auth_start, "auth/poll": api.auth_poll,
                    "logout": api.logout, "catalog": api.catalog, "install": api.install,
                    "settings": api.settings, "updates/run": api.automatic_updates,
                    "uninstall": api.uninstall, "enabled": api.enabled}
        with_body = {"auth/poll", "install", "settings", "uninstall", "enabled"}
        # Native operations sometimes print status; only our public envelope leaves.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = asyncio.run(handlers[args.operation](body) if args.operation in with_body
                                 else handlers[args.operation]())
        if args.operation == "state":
            from hermes_cli.plugins_cmd import _get_enabled_set, _get_disabled_set
            record = api._metadata().get(api.PLUGIN_ID, {})
            result["backend"] = {"enabled": api.PLUGIN_ID in _get_enabled_set() and api.PLUGIN_ID not in _get_disabled_set(),
                                 "source": record.get("source"), "revision": record.get("revision"), "pinned": record.get("pinned")}
        payload = {"ok": True, "result": {**result, "target": target}}
    except api.HTTPException as exc:
        # Details may include remote error bodies/installer output. Never forward
        # those via CLI logs; fixed messages for auth and upstream failures.
        detail = str(exc.detail) if exc.status_code in {400, 404, 409, 410, 429} else "Marketplace operation failed; check GitHub access and retry"
        payload = {"ok": False, "status": exc.status_code, "error": detail}
    except Exception:
        payload = {"ok": False, "status": 500, "error": "Marketplace operation failed safely; retry or reconnect GitHub"}
    encoded = json.dumps(payload, separators=(",", ":"))
    if len(encoded) > 46000:
        encoded = json.dumps({"ok": False, "status": 413, "error": "Marketplace response exceeds native transport limit"})
    print("AUTOMATION_LAB_JSON:" + encoded)


def register(ctx):
    ctx.register_cli_command("automation-lab", "Automation Lab Marketplace administration", _parser, _run)
