"""Exercise real Git event diffs without a GitHub account or shared state."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

HELPER = Path(__file__).resolve().parents[1] / "ops" / "ci_scope.py"


class EventScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Synthetic Test")
        (self.root / "Dockerfile.library").write_text("FROM scratch\n")
        (self.root / "README.md").write_text("Synthetic documentation\n")
        self.base = self.commit()

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, stderr=subprocess.PIPE).decode().strip()

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-qm", "Synthetic event fixture")
        return self.git("rev-parse", "HEAD")

    def run_event(self, name, event):
        event_path = self.root / "event.json"
        output = self.root / "output"
        event_path.write_text(json.dumps(event))
        subprocess.run(["python3", str(HELPER), name, str(event_path), str(output)], cwd=self.root, check=True, env={**os.environ, "BEADS_DIR": str(self.root / "nonexistent-beads")})
        return dict(line.split("=") for line in output.read_text().splitlines())

    def pr(self, head):
        return {"pull_request": {"base": {"sha": self.base}, "head": {"sha": head, "repo": {"fork": True}}}}

    def test_fork_documentation_completes_without_heavy_jobs(self):
        (self.root / "README.md").write_text("Changed documentation\n")
        self.assertEqual(set(self.run_event("pull_request", self.pr(self.commit())).values()), {"false"})

    def test_rename_out_of_runtime_into_docs_keeps_old_path(self):
        self.git("mv", "Dockerfile.library", "reader.md")
        self.assertEqual(self.run_event("pull_request", self.pr(self.commit())), {"worker": "false", "library": "true", "byparr": "false"})

    def test_push_uses_complete_range(self):
        (self.root / "unknown.file").write_text("Unclassified\n")
        head = self.commit()
        self.assertEqual(set(self.run_event("push", {"before": self.base, "after": head}).values()), {"true"})

    def test_rename_into_runtime_includes_new_path(self):
        self.git("mv", "README.md", "Dockerfile.byparr")
        self.assertEqual(self.run_event("pull_request", self.pr(self.commit())), {"worker": "false", "library": "false", "byparr": "true"})

    def test_multi_commit_push_keeps_an_earlier_runtime_change(self):
        (self.root / "Dockerfile.library").write_text("FROM scratch\n# reader change\n")
        self.commit()
        (self.root / "README.md").write_text("Later docs change\n")
        self.assertEqual(self.run_event("push", {"before": self.base, "after": self.commit()}), {"worker": "false", "library": "true", "byparr": "false"})

    def test_missing_fork_history_runs_full(self):
        self.assertEqual(set(self.run_event("pull_request", self.pr("1" * 40)).values()), {"true"})

    def test_schedule_runs_full(self):
        self.assertEqual(set(self.run_event("schedule", {}).values()), {"true"})

    def test_shared_library_module_rebuilds_application_and_reader(self):
        (self.root / "library").mkdir()
        (self.root / "library/catalog.py").write_text("# synthetic shared runtime module\n")
        self.assertEqual(self.run_event("pull_request", self.pr(self.commit())),
                         {"worker": "true", "library": "true", "byparr": "false"})

    def test_untrusted_sha_does_not_become_a_shell_command(self):
        self.assertEqual(set(self.run_event("pull_request", self.pr("$(touch injected)")).values()), {"true"})
        self.assertFalse((self.root / "injected").exists())


class RequiredResultTests(unittest.TestCase):
    def run_gate(self, selected, overrides=None):
        results = {"scope": {"result": "success", "outputs": dict.fromkeys(("worker", "library", "byparr"), str(selected).lower())}, "policy": {"result": "success"}}
        for name in ("offline-tests", "worker-image", "library-image", "byparr-image"):
            results[name] = {"result": "success" if selected else "skipped"}
        for name, value in (overrides or {}).items():
            results[name]["result"] = value
        return subprocess.run(["python3", str(HELPER), "--check-results", json.dumps(results)], capture_output=True).returncode

    def test_documentation_scope_terminates_successfully(self):
        self.assertEqual(self.run_gate(False), 0)

    def test_selected_images_and_tests_succeed(self):
        self.assertEqual(self.run_gate(True), 0)

    def test_selected_job_skip_failure_or_cancellation_blocks(self):
        for result in ("skipped", "failure", "cancelled"):
            with self.subTest(result=result):
                self.assertNotEqual(self.run_gate(True, {"library-image": result}), 0)

    def test_policy_or_scope_failure_blocks_docs(self):
        for job in ("scope", "policy"):
            self.assertNotEqual(self.run_gate(False, {job: "failure"}), 0)


if __name__ == "__main__":
    unittest.main()
