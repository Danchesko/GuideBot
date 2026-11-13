import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("scripts/collect-city.sh", "manage.sh"):
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, self.root / name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.calls = self.root / "calls"
        self.env = {
            **os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}",
            "CALLS": str(self.calls), "RUNNING": "0", "RUN_EXIT": "0", "START_EXIT": "0", "ONEOFF": "False",
        }
        self.command("python", """
echo "python $*" >> "$CALLS"
case " $* " in *" -m "*) exit "$RUN_EXIT" ;; *) exit 0 ;; esac
""")
        self.command("docker", """
echo "docker $*" >> "$CALLS"
case "$*" in
  "compose ps --status running -q bot") if [ "$RUNNING" = 1 ]; then echo container-id; fi ;;
  "compose run "*) exit "$RUN_EXIT" ;;
  "compose up "*) exit "$START_EXIT" ;;
  "inspect "*) echo "$ONEOFF" ;;
  *) exit 0 ;;
esac
""")

    def command(self, name, body):
        file = self.bin / name
        file.write_text("#!/bin/bash\n" + body + "\n")
        file.chmod(0o755)

    def run_script(self, script, *args):
        return subprocess.run(
            ["bash", str(self.root / script), *args],
            env=self.env, capture_output=True, text=True, timeout=10,
        )

    def log(self):
        return self.calls.read_text() if self.calls.exists() else ""

    def test_invalid_update_arguments_have_no_side_effects(self):
        for args in [("unknown",), ("almaty", "--step", "unknown"),
                     ("almaty", "--step", "trust", "--from", "reviews")]:
            with self.subTest(args=args):
                self.assertNotEqual(self.run_script("scripts/collect-city.sh", *args).returncode, 0)
                self.assertEqual(self.log(), "")

    def test_collector_has_no_service_management(self):
        result = self.run_script("scripts/collect-city.sh", "almaty", "--step", "trust")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("python -m bishkek_food_finder.indexer.trust", self.log())
        self.assertNotIn("docker", self.log())

    def test_collector_failure_keeps_exit_status(self):
        self.env["RUN_EXIT"] = "7"
        result = self.run_script("scripts/collect-city.sh", "almaty", "--step", "trust")
        self.assertEqual(result.returncode, 7)
        self.assertIn("./manage.sh update", result.stdout)

    def test_from_trust_runs_both_indexing_steps(self):
        result = self.run_script("scripts/collect-city.sh", "almaty", "--from", "trust")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("indexer.trust", self.log())
        self.assertIn("indexer.embeddings", self.log())
        self.assertNotIn("scraper.", self.log())

    def test_data_lock_rejects_second_collector(self):
        (self.root / "data").mkdir()
        with (self.root / "data/.update.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_script("scripts/collect-city.sh", "almaty", "--step", "trust")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("another data update", result.stderr)
        self.assertEqual(self.log(), "")

    def test_manager_preserves_stopped_bot(self):
        result = self.run_script("manage.sh", "update", "almaty", "--step", "trust")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--interactive=false collector", self.log())
        self.assertNotIn("compose up", self.log())
        self.assertNotIn("compose stop", self.log())

    def test_manager_restarts_running_bot_after_failure(self):
        self.env.update(RUNNING="1", RUN_EXIT="7")
        result = self.run_script("manage.sh", "update", "almaty", "--step", "trust")
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertIn("compose stop -t 60 bot", self.log())
        self.assertIn("compose up -d --no-deps --no-recreate --wait", self.log())

    def test_manager_reports_restart_failure(self):
        self.env.update(RUNNING="1", START_EXIT="1")
        result = self.run_script("manage.sh", "update", "almaty", "--step", "trust")
        self.assertNotEqual(result.returncode, 0)

    def test_manager_does_not_interrupt_one_off_bot_command(self):
        self.env.update(RUNNING="1", ONEOFF="True")
        result = self.run_script("manage.sh", "update", "almaty", "--step", "trust")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("one-off bot command", result.stderr)
        self.assertNotIn("compose stop", self.log())
        self.assertNotIn("compose run", self.log())

    def test_management_lock(self):
        with (self.root / ".manage.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_script("manage.sh", "stop")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.log(), "")


if __name__ == "__main__":
    unittest.main()
