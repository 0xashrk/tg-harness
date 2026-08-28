import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SUPERVISOR = ROOT / "scripts" / "supervise.sh"


class SupervisorTests(unittest.TestCase):
    def fake_path(self, curl_body: str, curl_exit: int = 0):
        tmp = tempfile.TemporaryDirectory()
        curl = Path(tmp.name) / "curl"
        curl.write_text(
            "#!/usr/bin/env bash\n"
            f"printf '%s\\n' {shlex.quote(curl_body)}\n"
            f"exit {curl_exit}\n"
        )
        curl.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = f"{tmp.name}:{env['PATH']}"
        return tmp, env

    def run_socks_up(self, env):
        command = f"source {shlex.quote(str(SUPERVISOR))}; socks_up"
        return subprocess.run(["bash", "-c", command], env=env, check=False)

    def test_socks_up_requires_warp_trace(self):
        tmp, env = self.fake_path("warp=on")
        with tmp:
            self.assertEqual(self.run_socks_up(env).returncode, 0)

    def test_socks_up_rejects_non_warp_listener(self):
        tmp, env = self.fake_path("warp=off")
        with tmp:
            self.assertNotEqual(self.run_socks_up(env).returncode, 0)

    def test_socks_up_rejects_partial_response_from_failed_request(self):
        tmp, env = self.fake_path("warp=on", curl_exit=7)
        with tmp:
            self.assertNotEqual(self.run_socks_up(env).returncode, 0)

    @unittest.skipUnless(shutil.which("flock"), "flock is Linux-only")
    def test_supervisor_lock_is_exclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            setup = (
                f"source {shlex.quote(str(SUPERVISOR))}; "
                f"OUT={shlex.quote(str(out))}; "
                'LOG="$OUT/supervisor.log"; '
                'LOCKFILE="$OUT/supervisor.lock"; '
            )
            holder = subprocess.Popen(
                ["bash", "-c", setup + 'acquire_lock; echo locked; sleep 5'],
                stdout=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(holder.stdout.readline().strip(), "locked")
                contender = subprocess.run(
                    ["bash", "-c", setup + "acquire_lock"],
                    check=False,
                )
                self.assertNotEqual(contender.returncode, 0)
            finally:
                holder.terminate()
                holder.wait(timeout=5)

    def test_no_dummy_local_socks_fallback(self):
        supervise = SUPERVISOR.read_text()
        restore = (ROOT / "scripts" / "restore-pipeline.sh").read_text()
        for body in (supervise, restore):
            self.assertNotIn("local_socks5", body)
            self.assertNotIn("dummy local SOCKS fallback", body)

    def test_restore_pipeline_is_executable_and_parses(self):
        restore = ROOT / "scripts" / "restore-pipeline.sh"
        self.assertTrue(restore.is_file())
        self.assertTrue(os.access(restore, os.X_OK))
        parsed = subprocess.run(["bash", "-n", str(restore)], check=False)
        self.assertEqual(parsed.returncode, 0)


if __name__ == "__main__":
    unittest.main()
