"""Isolated stock-Hermes install bridge with a Claude skill-pack adapter.

Only the temporary clone is adapted, before Hermes validates/scans/swaps it.
Original Claude/Codex files and upstream repositories are never modified.
"""
import argparse
import json
import re
from pathlib import Path

from plugin_api import _bounded_json


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
    from hermes_cli import plugins_cmd
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
    original = plugins_cmd._read_manifest_for_install
    def read(directory):
        prepare_manifest(directory, args.name)
        with (directory / ".marketplace-tree.json").open("x", encoding="utf-8") as stream:
            json.dump({"tree_sha": args.tree}, stream)
        return original(directory)
    # Process-local adapter at the pre-scan seam; never patches the installed runtime.
    plugins_cmd._read_manifest_for_install = read
    original_scan = plugins_cmd._scan_plugin_tree
    def scan(*positional, **options):
        try:
            return original_scan(*positional, **options)
        except plugins_cmd.PluginScanBlocked as error:
            if args.scan_report and error.scan_result is not None:
                with Path(args.scan_report).open("x", encoding="utf-8") as stream:
                    json.dump({"verdict": error.scan_result.verdict}, stream)
            raise
    plugins_cmd._scan_plugin_tree = scan
    plugins_cmd.cmd_install(args.identifier, force=args.force,
                            enable=args.enable and not args.no_enable, ref=args.ref)


if __name__ == "__main__":
    main()
