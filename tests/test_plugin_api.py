import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "dashboard"))
import plugin_api


class ScopedGitCredentialTest(unittest.TestCase):
    def test_git_store_is_host_scoped_and_installer_environment_is_clean(self):
        marker = "test-token-not-a-real-credential"
        with patch.dict(os.environ, {"GITHUB_TOKEN": "unrelated-token", "GH_TOKEN": "other-token"}):
            with plugin_api._askpass(marker) as env:
                token_file = Path(env["AUTOMATION_LAB_GITHUB_TOKEN_FILE"])
                self.assertIn(marker, token_file.read_text())
                if os.name != "nt":
                    self.assertEqual(token_file.stat().st_mode & 0o777, 0o600)
                self.assertNotIn(marker, env.values())
                self.assertNotIn("AUTOMATION_LAB_GITHUB_TOKEN", env)
                self.assertNotIn("GITHUB_TOKEN", env)
                self.assertNotIn("GH_TOKEN", env)
                self.assertEqual(os.environ["GITHUB_TOKEN"], "unrelated-token")
                allowed = subprocess.run(["git", "credential", "fill"],
                                         input="protocol=https\nhost=github.com\n\n",
                                         env=env, capture_output=True, text=True)
                self.assertEqual(allowed.returncode, 0, allowed.stderr)
                self.assertIn(f"password={marker}", allowed.stdout)
                for request in ("protocol=https\nhost=redirect.example\n\n",
                                "protocol=https\nhost=redirect.example\npath=github.com\n\n"):
                    rejected = subprocess.run(["git", "credential", "fill"],
                                              input=request, env=env, capture_output=True, text=True)
                    self.assertNotEqual(rejected.returncode, 0)
                    self.assertNotIn(marker, rejected.stdout + rejected.stderr)
                child_env = {key: value for key, value in env.items()
                             if key != "AUTOMATION_LAB_GITHUB_TOKEN_FILE"}
                result = plugin_api._run(
                    [sys.executable, "-c", "import os,sys,subprocess; t=sys.stdin.readline().strip(); "
                     "assert t.startswith('test-'); "
                     "assert all(k not in os.environ for k in "
                     "('AUTOMATION_LAB_GITHUB_TOKEN','GITHUB_TOKEN','GH_TOKEN')); "
                     "assert sys.platform != 'darwin' or t not in "
                     "subprocess.run(['ps','eww','-p',str(os.getpid())], "
                     "capture_output=True,text=True,check=True).stdout; "
                     "print('safe')"],
                    env=child_env, input_text=marker + "\n")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "safe")
            self.assertFalse(token_file.exists())


if __name__ == "__main__":
    unittest.main()
