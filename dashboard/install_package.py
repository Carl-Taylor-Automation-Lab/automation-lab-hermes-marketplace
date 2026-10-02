"""Isolated stock-Hermes install bridge with a Claude skill-pack adapter.

Only the temporary clone is adapted, before Hermes validates/scans/swaps it.
Original Claude/Codex files and upstream repositories are never modified.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path
from unittest.mock import patch

from plugin_api import MARKETPLACES, _bounded_json


def prepare_manifest(directory: Path, expected_name: str) -> None:
    target = directory / "plugin.json"
    if not target.exists():
        source = _bounded_json(directory / ".claude-plugin/plugin.json", 128_000)
        if source.get("name") != expected_name:
            raise RuntimeError("Marketplace manifest name mismatch")
        # ponytail: adapt skill/MCP packs only, not Claude hooks or executable extensions.
        if any((directory / item).exists() for item in ("hooks", "commands", "__init__.py", "plugin.yaml")):
            raise RuntimeError("This Claude package needs a reviewed native Hermes adapter")
        manifest = {key: source[key] for key in
                    ("name", "version", "description", "author", "homepage", "license", "keywords") if key in source}
        manifest["$schema"] = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
        with target.open("x", encoding="utf-8") as stream:
            json.dump(manifest, stream)
        mcp_source = directory / ".mcp.json"
        if mcp_source.exists():
            payload = _bounded_json(mcp_source, 256_000)
            mcp_target = directory / "mcp.json"
            if mcp_target.exists():
                _bounded_json(mcp_target, 256_000)
            else:
                with mcp_target.open("x", encoding="utf-8") as stream:
                    json.dump(payload, stream)
    if _bounded_json(target, 128_000).get("name") != expected_name:
        raise RuntimeError("Marketplace package name mismatch")


def main():
    # ponytail: transport the credential through stdin, never a child-inspectable
    # launch environment. Git alone receives an origin-scoped header.
    token = sys.stdin.readline().rstrip("\n")
    if not token or "\r" in token:
        raise RuntimeError("Marketplace Git credential is missing or invalid")
    sys.stdin = open(os.devnull, "r", encoding="utf-8")
    from hermes_cli import git_credentials, plugins_cmd
    parser = argparse.ArgumentParser()
    parser.add_argument("identifier")
    parser.add_argument("--name", required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--tree", required=True)
    parser.add_argument("--scan-report")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--enable", action="store_true")
    parser.add_argument("--no-enable", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.tree):
        parser.error("--tree must be the exact package tree SHA")
    reader_name = ("_read_manifest_for_install"
                   if hasattr(plugins_cmd, "_read_manifest_for_install") else "_read_manifest")
    original_read = getattr(plugins_cmd, reader_name)
    def read(directory):
        prepare_manifest(directory, args.name)
        return original_read(directory)
    # Hermes 0.21.0 handled this inline; newer builds expose a dedicated seam.
    setattr(plugins_cmd, reader_name, read)
    # Current Hermes resolves the reader in its installer module, not the facade.
    install_module = sys.modules.get("hermes_cli.plugins_cmd_install")
    if install_module and hasattr(install_module, "_read_manifest_for_install"):
        setattr(install_module, "_read_manifest_for_install", read)
    original_scan = plugins_cmd._scan_plugin_tree
    def scan(directory, *positional, **options):
        prepare_manifest(directory, args.name)
        with (directory / ".marketplace-tree.json").open("x", encoding="utf-8") as stream:
            json.dump({"tree_sha": args.tree}, stream)
        try:
            return original_scan(directory, *positional, **options)
        except plugins_cmd.PluginScanBlocked as error:
            if args.scan_report and error.scan_result is not None:
                with Path(args.scan_report).open("x", encoding="utf-8") as stream:
                    json.dump({"verdict": error.scan_result.verdict}, stream)
            raise
    plugins_cmd._scan_plugin_tree = scan
    allowed = {f"https://github.com/{market['repo']}.git" for market in MARKETPLACES.values()}
    def scoped_credentials(url):
        if url in allowed:
            yield "Automation Lab OAuth", ("x-access-token", token)
    # ponytail: stock Git stays origin-scoped; no fallback to the member's broader gh login.
    with patch.object(git_credentials, "iter_git_basic_auth", scoped_credentials):
        plugins_cmd.cmd_install(args.identifier, force=args.force,
                                enable=args.enable and not args.no_enable, ref=args.ref)


if __name__ == "__main__":
    main()
