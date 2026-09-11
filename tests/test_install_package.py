import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "dashboard" / "install_package.py"


class InstallPackageCompatibilityTest(unittest.TestCase):
    def test_legacy_and_current_manifest_seams_prepare_before_scan(self):
        for newer in (False, True):
            with self.subTest(newer=newer), tempfile.TemporaryDirectory() as tmp:
                package = Path(tmp) / "ai-business-brain"
                (package / ".claude-plugin").mkdir(parents=True)
                (package / ".claude-plugin" / "plugin.json").write_text(json.dumps({
                    "name": "ai-business-brain", "version": "1.14.0",
                }))
                (package / ".mcp.json").write_text('{"mcpServers": {}}')

                plugins_cmd = types.ModuleType("hermes_cli.plugins_cmd")
                setattr(plugins_cmd, "PluginScanBlocked", type("PluginScanBlocked", (Exception,), {}))

                def read(directory):
                    target = directory / "plugin.json"
                    return json.loads(target.read_text()) if target.exists() else {}

                setattr(plugins_cmd, "_read_manifest", read)
                if newer:
                    setattr(plugins_cmd, "_read_manifest_for_install", read)
                observed = {}

                def scan(directory, *_args, **_kwargs):
                    observed["manifest"] = json.loads((directory / "plugin.json").read_text())
                    observed["mcp"] = json.loads((directory / "mcp.json").read_text())
                    observed["tree"] = json.loads((directory / ".marketplace-tree.json").read_text())

                setattr(plugins_cmd, "_scan_plugin_tree", scan)

                def install(_identifier, **_kwargs):
                    reader = getattr(plugins_cmd, "_read_manifest_for_install", plugins_cmd._read_manifest)
                    observed["returned"] = reader(package)
                    plugins_cmd._scan_plugin_tree(package, "fixture", force=False)

                setattr(plugins_cmd, "cmd_install", install)
                hermes_cli = types.ModuleType("hermes_cli")
                setattr(hermes_cli, "plugins_cmd", plugins_cmd)
                plugin_api = types.ModuleType("plugin_api")
                setattr(plugin_api, "_bounded_json", lambda path, _limit: json.loads(path.read_text()))

                spec = importlib.util.spec_from_file_location(f"install_package_{newer}", SCRIPT)
                assert spec and spec.loader
                module = importlib.util.module_from_spec(spec)
                argv = [
                    str(SCRIPT), "owner/repo/plugins/ai-business-brain",
                    "--name", "ai-business-brain", "--ref", "0" * 40, "--tree", "1" * 40,
                ]
                with patch.dict(sys.modules, {"hermes_cli": hermes_cli, "plugin_api": plugin_api}), patch.object(sys, "argv", argv):
                    spec.loader.exec_module(module)
                    module.main()

                self.assertEqual(observed["returned"]["name"], "ai-business-brain")
                self.assertEqual(observed["manifest"], observed["returned"])
                self.assertEqual(observed["mcp"], {"mcpServers": {}})
                self.assertEqual(observed["tree"], {"tree_sha": "1" * 40})


if __name__ == "__main__":
    unittest.main()
