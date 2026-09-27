#!/usr/bin/env python3
"""Integration tests for cursor-handoff (fake agent only; no live API)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
HANDOFF = ROOT / "skills" / "cursor-handoff" / "scripts" / "handoff.py"
WATCH = ROOT / "skills" / "cursor-handoff" / "scripts" / "watch.py"
PROGRESS = ROOT / "skills" / "cursor-handoff" / "scripts" / "progress.py"
INSTALL = ROOT / "install.py"
PACKAGE = ROOT / "scripts" / "package_release.py"
FAKE_AGENT = Path(__file__).resolve().parent / "fake_agent.py"


def run_py(args: list[str], env: dict | None = None, cwd: Path | None = None, timeout: float = 60.0):
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        [sys.executable, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd) if cwd else None,
        env=merged,
        timeout=timeout,
        shell=False,
    )


def make_agent_launcher(dir_path: Path) -> Path:
    """Create a portable fake agent path suitable for --agent-path."""
    dir_path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        ps1 = dir_path / "agent.ps1"
        ps1.write_text(
            "\n".join(
                [
                    "$ErrorActionPreference = 'Stop'",
                    "if (-not $env:FAKE_AGENT_PY) { Write-Error 'FAKE_AGENT_PY missing'; exit 1 }",
                    "$py = if ($env:FAKE_AGENT_PYTHON) { $env:FAKE_AGENT_PYTHON } else { 'python' }",
                    "& $py $env:FAKE_AGENT_PY @args",
                    "exit $LASTEXITCODE",
                    "",
                ]
            ),
            encoding="utf-8",
            newline="\n",
        )
        return ps1
    script = dir_path / "agent"
    script.write_text(
        "#!/bin/sh\n"
        f"exec {shlex.quote(sys.executable)} {shlex.quote(str(FAKE_AGENT))} \"$@\"\n",
        encoding="utf-8",
        newline="\n",
    )
    script.chmod(0o755)
    return script


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        proc = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"],
            capture_output=True,
            text=True,
            shell=False,
        )
        return str(pid) in (proc.stdout or "")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class HandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name).resolve()
        self.workspace = self.tmp / "workspace"
        self.workspace.mkdir()
        self.agent_dir = self.tmp / "agent"
        self.agent_dir.mkdir()
        self.agent = make_agent_launcher(self.agent_dir)
        self.base_env = {
            "FAKE_AGENT_PY": str(FAKE_AGENT),
            "FAKE_AGENT_PYTHON": sys.executable,
            "FAKE_AGENT_MODE": "success",
            "FAKE_AGENT_SLEEP": "0",
        }

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def write_task(self, name: str = "task.md", body: str = "# Task\n\nDo the thing.\n") -> Path:
        path = self.workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    def run_handoff(self, task: Path, extra: list[str] | None = None, env: dict | None = None, timeout: float = 60.0):
        args = [
            str(HANDOFF),
            "--workspace",
            str(self.workspace),
            "--task",
            str(task),
            "--agent-path",
            str(self.agent),
            "--timeout",
            "30",
        ]
        if extra:
            args.extend(extra)
        merged = dict(self.base_env)
        if env:
            merged.update(env)
        return run_py(args, env=merged, timeout=timeout)

    def latest_run(self) -> Path:
        runs = sorted((self.workspace / ".cursor-handoff").glob("*"))
        runs = [p for p in runs if p.is_dir()]
        self.assertTrue(runs, "expected a run directory")
        return runs[-1]

    def latest_status(self) -> dict:
        return json.loads((self.latest_run() / "status.json").read_text(encoding="utf-8"))

    def test_success(self) -> None:
        task = self.write_task()
        proc = self.run_handoff(task)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        status = self.latest_status()
        self.assertEqual(status["state"], "execution_completed")
        self.assertEqual(status.get("session_id"), "sess-ok")
        self.assertTrue(Path(status["result_file"]).is_file())
        self.assertFalse((self.workspace / ".cursor-handoff" / "workspace.lock").exists())

    def test_error_result(self) -> None:
        task = self.write_task()
        proc = self.run_handoff(task, env={"FAKE_AGENT_MODE": "error"})
        self.assertNotEqual(proc.returncode, 0)
        status = self.latest_status()
        self.assertEqual(status["state"], "failed")
        self.assertTrue(status.get("agent_is_error"))

    def test_missing_result(self) -> None:
        task = self.write_task()
        proc = self.run_handoff(task, env={"FAKE_AGENT_MODE": "missing"})
        self.assertNotEqual(proc.returncode, 0)
        status = self.latest_status()
        self.assertEqual(status["state"], "failed")
        self.assertIn("Missing result", status.get("error", ""))

    def test_nonzero_exit(self) -> None:
        task = self.write_task()
        proc = self.run_handoff(task, env={"FAKE_AGENT_MODE": "nonzero"})
        self.assertNotEqual(proc.returncode, 0)
        status = self.latest_status()
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status.get("exit_code"), 7)

    def test_timeout(self) -> None:
        task = self.write_task()
        proc = self.run_handoff(
            task,
            extra=["--timeout", "1"],
            env={"FAKE_AGENT_MODE": "success", "FAKE_AGENT_SLEEP": "5"},
            timeout=30.0,
        )
        self.assertNotEqual(proc.returncode, 0)
        status = self.latest_status()
        self.assertEqual(status["state"], "timeout")
        self.assertIn("finished_at", status)

    def test_workspace_lock(self) -> None:
        lock_dir = self.workspace / ".cursor-handoff"
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock = lock_dir / "workspace.lock"
        lock.write_text(json.dumps({"pid": 1, "run_id": "other"}), encoding="utf-8")
        task = self.write_task()
        proc = self.run_handoff(task)
        self.assertNotEqual(proc.returncode, 0)
        self.assertTrue(lock.exists(), "runner must not delete another lock")
        self.assertIn("lock", (proc.stdout + proc.stderr).lower())

    def test_path_escape_rejected(self) -> None:
        outside = self.tmp / "outside.md"
        outside.write_text("# nope\n", encoding="utf-8")
        proc = self.run_handoff(outside)
        self.assertNotEqual(proc.returncode, 0)
        blob = proc.stdout + proc.stderr
        self.assertIn("inside the workspace", blob.lower())

    def test_unicode_and_spaces_task_path(self) -> None:
        folder = self.workspace / "docs with spaces" / "작업"
        folder.mkdir(parents=True)
        task = folder / "할 일.md"
        task.write_text("# Unicode task\n\nImplement toy change.\n", encoding="utf-8")
        proc = self.run_handoff(task)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        status = self.latest_status()
        self.assertEqual(status["state"], "execution_completed")

    def test_dry_run_no_mutation(self) -> None:
        task = self.write_task()
        before = list(self.workspace.rglob("*"))
        proc = self.run_handoff(task, extra=["--dry-run"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        after = list(self.workspace.rglob("*"))
        self.assertEqual(before, after)
        self.assertNotIn("# Task", proc.stdout)
        self.assertIn("dry-run", proc.stdout)

    def test_reject_cmd_override(self) -> None:
        bad = self.agent_dir / "agent.cmd"
        bad.write_text("@echo off\n", encoding="utf-8")
        # Place a valid fallback that must NOT be used.
        good = make_agent_launcher(self.tmp / "fallback-agent")
        task = self.write_task()
        env = dict(self.base_env)
        env["CURSOR_AGENT_PATH"] = str(good)
        proc = run_py(
            [
                str(HANDOFF),
                "--workspace",
                str(self.workspace),
                "--task",
                str(task),
                "--agent-path",
                str(bad),
            ],
            env=env,
        )
        self.assertNotEqual(proc.returncode, 0)
        blob = (proc.stdout + proc.stderr).lower()
        self.assertIn("unsafe", blob)
        self.assertFalse((self.workspace / ".cursor-handoff").exists())

    def test_missing_agent_path_no_fallback(self) -> None:
        missing = self.agent_dir / "missing-agent"
        good = make_agent_launcher(self.tmp / "fallback-agent")
        task = self.write_task()
        env = dict(self.base_env)
        env["CURSOR_AGENT_PATH"] = str(good)
        proc = run_py(
            [
                str(HANDOFF),
                "--workspace",
                str(self.workspace),
                "--task",
                str(task),
                "--agent-path",
                str(missing),
            ],
            env=env,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("not found", (proc.stdout + proc.stderr).lower())

    def test_cursor_agent_path_authoritative(self) -> None:
        missing = self.tmp / "no-such-agent"
        task = self.write_task()
        env = dict(self.base_env)
        env["CURSOR_AGENT_PATH"] = str(missing)
        # Also put a good agent on PATH-style discovery location; override must win.
        proc = run_py(
            [
                str(HANDOFF),
                "--workspace",
                str(self.workspace),
                "--task",
                str(task),
                "--timeout",
                "30",
            ],
            env=env,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("cursor_agent_path", (proc.stdout + proc.stderr).lower())

    def test_discovery_fallback_path(self) -> None:
        path_dir = self.tmp / "bin"
        path_dir.mkdir()
        if os.name == "nt":
            target = path_dir / "cursor-agent.ps1"
            shutil.copy2(self.agent, target)
            env = dict(self.base_env)
            env["CURSOR_AGENT_PATH"] = str(target)
            task = self.write_task()
            proc = run_py(
                [
                    str(HANDOFF),
                    "--workspace",
                    str(self.workspace),
                    "--task",
                    str(task),
                    "--timeout",
                    "30",
                ],
                env=env,
            )
        else:
            target = path_dir / "cursor-agent"
            shutil.copy2(self.agent, target)
            target.chmod(0o755)
            env = dict(self.base_env)
            env.pop("CURSOR_AGENT_PATH", None)
            env["PATH"] = str(path_dir) + os.pathsep + env.get("PATH", "")
            task = self.write_task()
            proc = run_py(
                [
                    str(HANDOFF),
                    "--workspace",
                    str(self.workspace),
                    "--task",
                    str(task),
                    "--timeout",
                    "30",
                ],
                env=env,
            )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_probe_incomplete_help_no_dispatch(self) -> None:
        task = self.write_task()
        proc = self.run_handoff(task, env={"FAKE_AGENT_MODE": "help_incomplete"})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("required flags", (proc.stdout + proc.stderr).lower())
        handoff = self.workspace / ".cursor-handoff"
        if handoff.exists():
            runs = [p for p in handoff.iterdir() if p.is_dir()]
            self.assertEqual(runs, [], "probe failure must not create a run directory")

    def test_probe_help_fail_doctor_nonzero(self) -> None:
        proc = run_py(
            [str(HANDOFF), "--doctor", "--agent-path", str(self.agent)],
            env={**self.base_env, "FAKE_AGENT_MODE": "help_fail"},
        )
        self.assertNotEqual(proc.returncode, 0)
        report = json.loads(proc.stdout)
        self.assertFalse(report.get("ok", True))

    def test_dispatch_uses_frozen_task_snapshot(self) -> None:
        task = self.write_task(body="# Original\n\nStay frozen.\n")
        record = self.tmp / "agent-record.json"
        proc = self.run_handoff(task, env={"FAKE_AGENT_RECORD": str(record)})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(record.read_text(encoding="utf-8"))
        task_path = payload["task_path"]
        self.assertIsNotNone(task_path)
        self.assertIn(".cursor-handoff/", task_path.replace("\\", "/"))
        self.assertTrue(task_path.replace("\\", "/").endswith("/task.md"))
        # Original edits after dispatch must not matter; snapshot content is frozen.
        run = self.latest_run()
        self.assertEqual((run / "task.md").read_text(encoding="utf-8"), "# Original\n\nStay frozen.\n")

    def test_malformed_result_not_success(self) -> None:
        task = self.write_task()
        proc = self.run_handoff(task, env={"FAKE_AGENT_MODE": "malformed_result"})
        self.assertNotEqual(proc.returncode, 0)
        status = self.latest_status()
        self.assertEqual(status["state"], "failed")
        self.assertTrue(status.get("agent_is_error"))

    def test_array_event_ignored_then_success(self) -> None:
        task = self.write_task()
        proc = self.run_handoff(task, env={"FAKE_AGENT_MODE": "array_event"})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        status = self.latest_status()
        self.assertEqual(status["state"], "execution_completed")
        self.assertEqual(status.get("session_id"), "sess-after-array")

    def test_prior_error_not_erased_by_success(self) -> None:
        task = self.write_task()
        proc = self.run_handoff(task, env={"FAKE_AGENT_MODE": "error_then_success"})
        self.assertNotEqual(proc.returncode, 0)
        status = self.latest_status()
        self.assertEqual(status["state"], "failed")
        self.assertTrue(status.get("agent_is_error"))

    def test_late_result_preserves_timeout(self) -> None:
        task = self.write_task()
        proc = self.run_handoff(
            task,
            extra=["--timeout", "1"],
            env={"FAKE_AGENT_MODE": "late_result_after_sleep", "FAKE_AGENT_SLEEP": "5"},
            timeout=30.0,
        )
        self.assertNotEqual(proc.returncode, 0)
        status = self.latest_status()
        self.assertEqual(status["state"], "timeout")

    def test_reader_failure_propagates(self) -> None:
        import gc
        import importlib.util
        import warnings

        # Fresh import so prior test mutations cannot leak into this case.
        mod_name = f"handoff_under_test_{id(self)}"
        spec = importlib.util.spec_from_file_location(mod_name, HANDOFF)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        original = mod.atomic_write_json

        def boom(path: Path, data: dict) -> None:
            if path.name == "result.json":
                raise OSError("simulated result write failure")
            return original(path, data)

        mod.atomic_write_json = boom  # type: ignore[assignment]
        try:
            task = self.write_task()
            ns = mod.build_parser().parse_args(
                [
                    "--workspace",
                    str(self.workspace),
                    "--task",
                    str(task),
                    "--agent-path",
                    str(self.agent),
                    "--timeout",
                    "30",
                ]
            )
            old = dict(os.environ)
            os.environ.update(self.base_env)
            try:
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always", ResourceWarning)
                    code = mod.run_handoff(ns)
                    gc.collect()
            finally:
                os.environ.clear()
                os.environ.update(old)
            self.assertNotEqual(code, 0)
            status = self.latest_status()
            self.assertEqual(status["state"], "runner_error")
            self.assertIn("Reader failure", status.get("error", ""))
            stdout_leaks = [
                w
                for w in caught
                if issubclass(w.category, ResourceWarning)
                and "stdout" in str(w.message).lower()
            ]
            self.assertEqual(stdout_leaks, [], f"unexpected ResourceWarning: {stdout_leaks}")
        finally:
            mod.atomic_write_json = original  # type: ignore[assignment]
            sys.modules.pop(mod_name, None)

    def test_unicode_workspace_legacy_stdout(self) -> None:
        ws = self.tmp / "작업공간"
        ws.mkdir()
        task = ws / "task.md"
        task.write_text("# Task\n\nDo the thing.\n", encoding="utf-8")
        env = dict(self.base_env)
        env["PYTHONIOENCODING"] = "cp949"
        proc = run_py(
            [
                str(HANDOFF),
                "--workspace",
                str(ws),
                "--task",
                str(task),
                "--agent-path",
                str(self.agent),
                "--timeout",
                "30",
            ],
            env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        status_line = [ln for ln in proc.stdout.splitlines() if "execution_completed" in ln][-1]
        parsed = json.loads(status_line)
        self.assertEqual(parsed["state"], "execution_completed")

    def test_child_process_cleaned_on_timeout(self) -> None:
        task = self.write_task()
        marker = self.tmp / "child-pid.txt"
        proc = self.run_handoff(
            task,
            extra=["--timeout", "2"],
            env={
                "FAKE_AGENT_MODE": "child_hang",
                "FAKE_AGENT_SLEEP": "0",
                "FAKE_AGENT_CHILD_SLEEP": "60",
                "FAKE_AGENT_CHILD_MARKER": str(marker),
            },
            timeout=45.0,
        )
        self.assertNotEqual(proc.returncode, 0)
        status = self.latest_status()
        self.assertEqual(status["state"], "timeout")
        self.assertTrue(marker.exists(), "child should have started")
        child_pid = int(marker.read_text(encoding="utf-8").strip())
        # Allow a short grace period for taskkill / killpg to finish.
        deadline = time.time() + 10
        while time.time() < deadline and pid_alive(child_pid):
            time.sleep(0.2)
        self.assertFalse(pid_alive(child_pid), f"descendant pid {child_pid} still running")

    def test_escaping_handoff_symlink_rejected(self) -> None:
        if os.name == "nt":
            # Directory junctions/symlinks often need elevation; skip if unavailable.
            outside = self.tmp / "outside-handoff"
            outside.mkdir()
            target = self.workspace / ".cursor-handoff"
            try:
                subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(target), str(outside)],
                    check=True,
                    capture_output=True,
                    shell=False,
                )
            except (OSError, subprocess.CalledProcessError):
                self.skipTest("Windows junction creation unavailable")
        else:
            outside = self.tmp / "outside-handoff"
            outside.mkdir()
            (self.workspace / ".cursor-handoff").symlink_to(outside, target_is_directory=True)
        task = self.write_task()
        proc = self.run_handoff(task)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("outside", (proc.stdout + proc.stderr).lower())


class VisibleExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name).resolve()
        self.workspace = self.tmp / "workspace"
        self.workspace.mkdir()
        self.agent_dir = self.tmp / "agent"
        self.agent_dir.mkdir()
        self.agent = make_agent_launcher(self.agent_dir)
        self.base_env = {
            "FAKE_AGENT_PY": str(FAKE_AGENT),
            "FAKE_AGENT_PYTHON": sys.executable,
            "FAKE_AGENT_MODE": "success",
            "FAKE_AGENT_SLEEP": "0",
        }
        scripts = str(ROOT / "skills" / "cursor-handoff" / "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def write_task(self, body: str = "# Task\n\nDo the thing.\n") -> Path:
        path = self.workspace / "task.md"
        path.write_text(body, encoding="utf-8")
        return path

    def run_handoff(self, task: Path, extra: list[str] | None = None, env: dict | None = None):
        args = [
            str(HANDOFF),
            "--workspace",
            str(self.workspace),
            "--task",
            str(task),
            "--agent-path",
            str(self.agent),
            "--timeout",
            "30",
        ]
        if extra:
            args.extend(extra)
        merged = dict(self.base_env)
        if env:
            merged.update(env)
        return run_py(args, env=merged, timeout=60.0)

    def latest_run(self) -> Path:
        runs = sorted(
            p for p in (self.workspace / ".cursor-handoff").iterdir() if p.is_dir()
        )
        self.assertTrue(runs)
        return runs[-1]

    def test_live_progress_before_completion(self) -> None:
        task = self.write_task()
        proc = self.run_handoff(
            task,
            extra=["--live"],
            env={
                "FAKE_AGENT_MODE": "progress_events",
                "FAKE_AGENT_PROGRESS_SLEEP": "0.3",
            },
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        # Progress is on stderr; machine JSON on stdout.
        self.assertIn("[handoff] workspace:", proc.stderr)
        self.assertIn("trust_workspace: False", proc.stderr)
        self.assertIn("tool shell", proc.stderr.lower())
        self.assertNotIn("secret chain of thought", proc.stderr)
        self.assertNotIn("secret payload", proc.stderr)
        self.assertNotIn("also must not render", proc.stderr)
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        self.assertGreaterEqual(len(lines), 2)
        running = json.loads(lines[0])
        final = json.loads(lines[-1])
        self.assertEqual(running["state"], "running")
        self.assertEqual(final["state"], "execution_completed")
        # Artifacts preserved.
        run = self.latest_run()
        self.assertTrue((run / "events.jsonl").is_file())
        self.assertTrue((run / "stderr.log").is_file())
        self.assertTrue((run / "result.json").is_file())

    def test_trust_denial_blocked_not_unrelated_stderr(self) -> None:
        task = self.write_task()
        denied = self.run_handoff(task, env={"FAKE_AGENT_MODE": "trust_denied"})
        self.assertNotEqual(denied.returncode, 0)
        status = json.loads((self.latest_run() / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["state"], "blocked")
        self.assertEqual(status.get("block_reason"), "trust_required")
        self.assertIn("trust", (status.get("next_action") or "").lower())

        # Clean for second run.
        shutil.rmtree(self.workspace / ".cursor-handoff")
        unrelated = self.run_handoff(task, env={"FAKE_AGENT_MODE": "unrelated_stderr"})
        self.assertEqual(unrelated.returncode, 0, unrelated.stdout + unrelated.stderr)
        status2 = json.loads((self.latest_run() / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(status2["state"], "execution_completed")
        self.assertNotEqual(status2.get("state"), "blocked")

    def test_trust_denial_does_not_override_timeout(self) -> None:
        import importlib.util

        from progress import detect_trust_denial

        self.assertTrue(detect_trust_denial("User declined workspace trust.\n"))
        self.assertFalse(
            detect_trust_denial("FAIL: test_trust_helper assertion failed\n")
        )

        spec = importlib.util.spec_from_file_location("handoff_vis", HANDOFF)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        protected = {"state": "timeout"}
        mod.apply_trust_denial_if_needed(
            protected, "Workspace Trust is required. User declined workspace trust."
        )
        self.assertEqual(protected["state"], "timeout")

        interrupted = {"state": "interrupted"}
        mod.apply_trust_denial_if_needed(
            interrupted, "Workspace Trust is required. User declined workspace trust."
        )
        self.assertEqual(interrupted["state"], "interrupted")

        blocked = {"state": "failed"}
        mod.apply_trust_denial_if_needed(
            blocked, "Workspace Trust is required. User declined workspace trust."
        )
        self.assertEqual(blocked["state"], "blocked")
        self.assertEqual(blocked["block_reason"], "trust_required")

        # Process killed during sleep before trust stderr: timeout retained.
        task = self.write_task()
        proc = self.run_handoff(
            task,
            extra=["--timeout", "1"],
            env={"FAKE_AGENT_MODE": "trust_denied", "FAKE_AGENT_SLEEP": "5"},
        )
        self.assertNotEqual(proc.returncode, 0)
        status = json.loads((self.latest_run() / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["state"], "timeout")

    def test_open_terminal_argv_windows_or_manual(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("handoff_term", HANDOFF)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        run_dir = self.workspace / ".cursor-handoff" / "fake-run"
        run_dir.mkdir(parents=True)
        (run_dir / "task.md").write_text("# t\n", encoding="utf-8")
        command = mod.build_watch_command(run_dir)
        self.assertEqual(command[0], sys.executable)
        self.assertEqual(command[1:3], ["-X", "utf8"])
        self.assertTrue(str(command[3]).endswith("watch.py"))
        self.assertEqual(command[4], "--run")
        self.assertEqual(command[5], str(run_dir))
        self.assertIn("--console", command)

        if os.name == "nt":
            captured: dict = {}

            def fake_popen(cmd, **kwargs):
                captured["cmd"] = cmd
                captured["kwargs"] = kwargs

                class P:
                    pid = 4242

                return P()

            original = mod.subprocess.Popen
            mod.subprocess.Popen = fake_popen  # type: ignore[assignment]
            try:
                info = mod.launch_open_terminal(run_dir)
            finally:
                mod.subprocess.Popen = original  # type: ignore[assignment]
            self.assertTrue(info["launched"])
            self.assertEqual(info.get("pid"), 4242)
            self.assertEqual(captured["cmd"], command)
            self.assertIs(captured["kwargs"].get("shell"), False)
            flags = captured["kwargs"].get("creationflags", 0)
            self.assertTrue(flags & mod.CREATE_NEW_CONSOLE)
            # Must not blank the console with DEVNULL/PIPE redirects.
            for handle in ("stdin", "stdout", "stderr"):
                self.assertNotIn(handle, captured["kwargs"])
                self.assertIsNot(
                    captured["kwargs"].get(handle),
                    mod.subprocess.DEVNULL,
                )
        else:
            info = mod.launch_open_terminal(run_dir)
            self.assertFalse(info["launched"])
            self.assertIn("manual_command", info)
            self.assertIn("watch.py", info["manual_command"])

            task = self.write_task()
            proc = self.run_handoff(task, extra=["--open-terminal"])
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            # fake-run sorts after dated run ids; use runner stdout JSON for the real path.
            lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
            self.assertTrue(lines, proc.stdout)
            final = json.loads(lines[-1])
            run_path = Path(final["run"])
            self.assertTrue(run_path.is_dir(), final)
            self.assertNotEqual(run_path.name, "fake-run")
            status = json.loads((run_path / "status.json").read_text(encoding="utf-8"))
            self.assertIn("viewer", status)
            self.assertIn("manual_command", status["viewer"])
            # open-terminal enables live on non-Windows
            self.assertIn("[handoff] workspace:", proc.stderr)

    def test_open_terminal_records_viewer_without_gui(self) -> None:
        """Patch launch so CI never opens a real console window."""
        import importlib.util

        spec = importlib.util.spec_from_file_location("handoff_viewer", HANDOFF)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        def fake_launch(run_dir: Path) -> dict:
            return {
                "command": mod.build_watch_command(run_dir),
                "command_display": "fake",
                "launched": os.name == "nt",
                "pid": 7777 if os.name == "nt" else None,
                "manual_command": None if os.name == "nt" else "fake-watch",
                "note": "test stub",
            }

        task = self.write_task()
        original = mod.launch_open_terminal
        mod.launch_open_terminal = fake_launch  # type: ignore[assignment]
        old = dict(os.environ)
        os.environ.update(self.base_env)
        try:
            ns = mod.build_parser().parse_args(
                [
                    "--workspace",
                    str(self.workspace),
                    "--task",
                    str(task),
                    "--agent-path",
                    str(self.agent),
                    "--timeout",
                    "30",
                    "--open-terminal",
                    "--live",
                ]
            )
            code = mod.run_handoff(ns)
        finally:
            mod.launch_open_terminal = original  # type: ignore[assignment]
            os.environ.clear()
            os.environ.update(old)
        self.assertEqual(code, 0)
        status = json.loads((self.latest_run() / "status.json").read_text(encoding="utf-8"))
        self.assertIn("viewer", status)
        if os.name == "nt":
            self.assertTrue(status["viewer"].get("launched"))
            self.assertEqual(status["viewer"].get("pid"), 7777)
        else:
            self.assertEqual(status["viewer"].get("manual_command"), "fake-watch")

    def test_watch_partial_and_completion_no_reasoning(self) -> None:
        import io
        import importlib.util
        import threading

        run_dir = self.workspace / ".cursor-handoff" / "watch-run"
        run_dir.mkdir(parents=True)
        (run_dir / "task.md").write_text("# Watch me\n\nBody.\n", encoding="utf-8")
        (run_dir / "status.json").write_text(
            json.dumps(
                {
                    "state": "running",
                    "workspace": str(self.workspace),
                    "trust_workspace": False,
                }
            ),
            encoding="utf-8",
        )
        events = run_dir / "events.jsonl"
        events.write_text("", encoding="utf-8")

        spec = importlib.util.spec_from_file_location("watch_mod", WATCH)
        assert spec and spec.loader
        watch_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(watch_mod)

        buf = io.StringIO()

        def writer() -> None:
            time.sleep(0.2)
            # Pid appears after meta_shown; watcher must still announce it.
            (run_dir / "status.json").write_text(
                json.dumps(
                    {
                        "state": "running",
                        "workspace": str(self.workspace),
                        "trust_workspace": False,
                        "pid": 424242,
                    }
                ),
                encoding="utf-8",
            )
            time.sleep(0.25)
            with events.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "type": "thinking",
                            "text": "hidden reasoning must not appear",
                        }
                    )
                    + "\n"
                )
                handle.write(
                    json.dumps(
                        {
                            "type": "tool_call",
                            "subtype": "started",
                            "tool_call": {"readToolCall": {"args": {"path": "x"}}},
                            "session_id": "sess-w",
                        }
                    )
                    + "\n"
                )
                handle.flush()
            time.sleep(0.2)
            (run_dir / "stderr.log").write_text("warn line\n", encoding="utf-8")
            (run_dir / "status.json").write_text(
                json.dumps(
                    {
                        "state": "execution_completed",
                        "session_id": "sess-w",
                        "exit_code": 0,
                        "workspace": str(self.workspace),
                        "trust_workspace": False,
                        "pid": 424242,
                        "finished_at": "2026-09-27T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )

        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        code = watch_mod.follow_run(run_dir, poll_s=0.15, stream=buf)
        thread.join(timeout=5)
        self.assertEqual(code, 0)
        out = buf.getvalue()
        self.assertIn("Watch me", out)
        self.assertIn("tool read", out.lower())
        self.assertIn("warn line", out)
        self.assertIn("execution_completed", out)
        self.assertIn("workspace:", out)
        self.assertIn("trust_workspace: False", out)
        self.assertIn("pid: 424242", out)
        self.assertNotIn("hidden reasoning", out)

    def test_watch_waits_for_finished_at_before_exit(self) -> None:
        """Transient terminal state without finished_at must not miss final trust block."""
        import io
        import importlib.util
        import threading

        run_dir = self.workspace / ".cursor-handoff" / "watch-trust-race"
        run_dir.mkdir(parents=True)
        (run_dir / "task.md").write_text("# Trust race\n", encoding="utf-8")
        (run_dir / "status.json").write_text(
            json.dumps(
                {
                    "state": "failed",
                    "workspace": str(self.workspace),
                    "trust_workspace": False,
                    "exit_code": 2,
                }
            ),
            encoding="utf-8",
        )

        spec = importlib.util.spec_from_file_location("watch_mod_race", WATCH)
        assert spec and spec.loader
        watch_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(watch_mod)

        buf = io.StringIO()

        def writer() -> None:
            time.sleep(0.35)
            (run_dir / "status.json").write_text(
                json.dumps(
                    {
                        "state": "blocked",
                        "block_reason": "trust_required",
                        "workspace": str(self.workspace),
                        "trust_workspace": False,
                        "exit_code": 2,
                        "error": "trust denied",
                        "next_action": "If you already declined/rejected workspace trust",
                        "finished_at": "2026-09-27T00:00:01+00:00",
                    }
                ),
                encoding="utf-8",
            )

        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        code = watch_mod.follow_run(run_dir, poll_s=0.1, stream=buf)
        thread.join(timeout=5)
        self.assertEqual(code, 0)
        out = buf.getvalue()
        self.assertIn("blocked", out)
        self.assertIn("trust_required", out)
        self.assertIn("workspace:", out)

    def test_progress_reporter_keeps_distinct_same_tool_calls(self) -> None:
        import io
        import progress as progress_mod

        buf = io.StringIO()
        reporter = progress_mod.ProgressReporter(buf, prefix_enabled=False)
        reporter.on_event(
            {
                "type": "tool_call",
                "subtype": "started",
                "call_id": "call-a",
                "tool_call": {"readToolCall": {"args": {"path": "a"}}},
            }
        )
        reporter.on_event(
            {
                "type": "tool_call",
                "subtype": "started",
                "call_id": "call-b",
                "tool_call": {"readToolCall": {"args": {"path": "b"}}},
            }
        )
        out = buf.getvalue()
        self.assertEqual(out.count("tool read"), 2)

    def test_tee_stderr_incremental_before_exit(self) -> None:
        """Small flushed stderr must surface via progress before the writer exits."""
        import importlib.util
        import io
        import threading

        spec = importlib.util.spec_from_file_location("handoff_tee", HANDOFF)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        read_fd, write_fd = os.pipe()
        reader = open(read_fd, "rb", buffering=0)
        writer = open(write_fd, "wb", buffering=0)
        stderr_path = self.workspace / "stderr-tee.log"
        seen: list[str] = []
        exited = threading.Event()
        progress_hit = threading.Event()

        class Probe:
            def on_stderr_text(self, text: str) -> None:
                seen.append(text)
                if "early-err" in text:
                    progress_hit.set()

        accumulated: list[str] = []
        errors: list = []
        thread = threading.Thread(
            target=mod.tee_stderr,
            args=(reader, stderr_path, Probe(), errors, accumulated),
            daemon=True,
        )
        thread.start()
        writer.write(b"early-err\n")
        writer.flush()
        # Prove progress saw the chunk while the peer is still alive.
        self.assertTrue(progress_hit.wait(timeout=2.0), "stderr not shown before exit")
        self.assertFalse(exited.is_set())
        time.sleep(0.05)
        # Split UTF-8 across writes (€ = e2 82 ac).
        writer.write(b"split-\xe2")
        writer.flush()
        time.sleep(0.05)
        writer.write(b"\x82\xac-done\n")
        writer.flush()
        writer.close()
        exited.set()
        thread.join(timeout=5)
        reader.close()
        self.assertFalse(errors)
        blob = "".join(accumulated)
        self.assertIn("early-err", blob)
        self.assertIn("€", blob)
        self.assertIn("early-err", stderr_path.read_text(encoding="utf-8"))

    def test_strip_c1_and_jsonl_byte_offsets(self) -> None:
        import progress as progress_mod

        cleaned = progress_mod.strip_control_chars("ok\x1b[31mX\x00\x9b\x9dY")
        self.assertNotIn("\x1b", cleaned)
        self.assertNotIn("\x00", cleaned)
        self.assertNotIn("\x9b", cleaned)
        self.assertNotIn("\x9d", cleaned)
        self.assertIn("ok", cleaned)
        self.assertIn("Y", cleaned)

        path = self.workspace / "events-offset.jsonl"
        # Invalid UTF-8 mid-file must not desync byte offsets across reads.
        line1 = b'{"type":"tool_call","subtype":"started","call_id":"c1",'
        line1 += b'"tool_call":{"shellToolCall":{}}}\n'
        bad = b'{"type":"assistant","note":"bad\xffbyte"}\n'
        line2 = b'{"type":"tool_call","subtype":"started","call_id":"c2",'
        line2 += b'"tool_call":{"shellToolCall":{}}}\n'
        path.write_bytes(line1)
        events, offset = progress_mod.iter_new_jsonl_events(path, 0)
        self.assertEqual(len(events), 1)
        self.assertEqual(offset, len(line1))
        with path.open("ab") as handle:
            handle.write(bad)
            handle.write(line2)
        events2, offset2 = progress_mod.iter_new_jsonl_events(path, offset)
        self.assertEqual(len(events2), 2)
        self.assertEqual(offset2, len(line1) + len(bad) + len(line2))
        # Partial trailing line left unread.
        with path.open("ab") as handle:
            handle.write(b'{"type":"tool_call","call_id":"partial"')
        events3, offset3 = progress_mod.iter_new_jsonl_events(path, offset2)
        self.assertEqual(events3, [])
        self.assertEqual(offset3, offset2)

    def test_trust_next_action_distinguishes_rejection(self) -> None:
        import progress as progress_mod

        fields = progress_mod.trust_block_fields()
        action = (fields.get("next_action") or "").lower()
        self.assertIn("declined", action)
        self.assertIn("never configured", action)
        self.assertIn(".workspace-trusted", (fields.get("next_action") or ""))
        self.assertNotIn("for this exact workspace only", action)

    def test_windows_viewer_console_handshake(self) -> None:
        """Real CREATE_NEW_CONSOLE viewer must attach CONIN$/CONOUT$ (isatty + content).

        Opt-in only: set CURSOR_HANDOFF_TEST_GUI=1 so normal unit tests/CI never
        open a real console window. Mocked launcher tests cover the default path.
        """
        if os.environ.get("CURSOR_HANDOFF_TEST_GUI", "").strip() not in {
            "1",
            "true",
            "yes",
        }:
            self.skipTest("Set CURSOR_HANDOFF_TEST_GUI=1 to run real Windows GUI handshake")
        if os.name != "nt":
            self.skipTest("Windows console handshake only")
        import importlib.util

        run_dir = self.workspace / ".cursor-handoff" / "console-handshake"
        run_dir.mkdir(parents=True)
        (run_dir / "task.md").write_text("# Console\n", encoding="utf-8")
        (run_dir / "status.json").write_text(
            json.dumps(
                {
                    "state": "running",
                    "workspace": str(self.workspace),
                    "trust_workspace": False,
                    "pid": 1,
                }
            ),
            encoding="utf-8",
        )

        spec = importlib.util.spec_from_file_location("handoff_console", HANDOFF)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        # Parent stdio is piped (like handoff when nested); viewer must still attach.
        old_hold = os.environ.get("CURSOR_HANDOFF_VIEWER_NO_HOLD")
        os.environ["CURSOR_HANDOFF_VIEWER_NO_HOLD"] = "1"
        try:
            info = mod.launch_open_terminal(run_dir)
        finally:
            if old_hold is None:
                os.environ.pop("CURSOR_HANDOFF_VIEWER_NO_HOLD", None)
            else:
                os.environ["CURSOR_HANDOFF_VIEWER_NO_HOLD"] = old_hold
        self.assertTrue(info.get("launched"), info)
        handshake = run_dir / "viewer-console.json"
        deadline = time.time() + 8.0
        while time.time() < deadline and not handshake.is_file():
            time.sleep(0.1)
        self.assertTrue(handshake.is_file(), "viewer-console.json missing")
        payload = json.loads(handshake.read_text(encoding="utf-8"))
        self.assertTrue(payload.get("attached"))
        self.assertTrue(payload.get("stdout_isatty"))
        self.assertTrue(payload.get("stdin_isatty"))
        self.assertIn("viewer console ready", payload.get("content", ""))
        viewer_pid = payload.get("pid")
        # Finalize so the viewer can leave follow_run; then kill to skip Enter hold.
        (run_dir / "status.json").write_text(
            json.dumps(
                {
                    "state": "execution_completed",
                    "workspace": str(self.workspace),
                    "trust_workspace": False,
                    "pid": 1,
                    "finished_at": "2026-09-27T00:00:02+00:00",
                    "exit_code": 0,
                }
            ),
            encoding="utf-8",
        )
        time.sleep(0.8)
        if isinstance(viewer_pid, int) and viewer_pid > 0:
            subprocess.run(
                ["taskkill", "/PID", str(viewer_pid), "/F", "/T"],
                capture_output=True,
                shell=False,
            )

    def test_summarize_strips_controls_and_skips_reasoning(self) -> None:
        import progress as progress_mod

        self.assertIsNone(
            progress_mod.summarize_event({"type": "thinking", "text": "nope"})
        )
        summary = progress_mod.summarize_event(
            {
                "type": "tool_call",
                "tool_call": {"shellToolCall": {"args": {"command": "echo hi"}}},
            }
        )
        self.assertIsNotNone(summary)
        assert summary is not None
        self.assertIn("shell", summary.lower())
        self.assertNotIn("echo hi", summary)
        cleaned = progress_mod.strip_control_chars("ok\x1b[31mX\x00")
        self.assertNotIn("\x1b", cleaned)
        self.assertNotIn("\x00", cleaned)


class InstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        # Canonical fixture root: macOS /var -> /private/var must not look like a user symlink.
        self.tmp = Path(self._tmpdir.name).resolve()

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_refuse_overwrite(self) -> None:
        dest_parent = self.tmp / "skills"
        dest = dest_parent / "cursor-handoff"
        dest.mkdir(parents=True)
        (dest / "SKILL.md").write_text("old\n", encoding="utf-8")
        proc = run_py(
            [
                str(INSTALL),
                "--target",
                "codex",
                "--scope",
                "user",
                "--destination",
                str(dest_parent),
            ]
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual((dest / "SKILL.md").read_text(encoding="utf-8"), "old\n")

    def test_update_backup_and_recovery(self) -> None:
        dest_parent = self.tmp / "skills"
        dest = dest_parent / "cursor-handoff"
        dest.mkdir(parents=True)
        (dest / "SKILL.md").write_text("old\n", encoding="utf-8")
        proc = run_py(
            [
                str(INSTALL),
                "--target",
                "claude",
                "--scope",
                "user",
                "--destination",
                str(dest_parent),
                "--update",
            ]
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("name: cursor-handoff", (dest / "SKILL.md").read_text(encoding="utf-8"))
        backup_root = dest_parent / ".cursor-handoff-backups"
        self.assertTrue(backup_root.is_dir())
        backups = list(backup_root.glob("*/cursor-handoff"))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "SKILL.md").read_text(encoding="utf-8"), "old\n")
        # Backup must not sit beside skills with a discoverable SKILL.md name.
        self.assertFalse(list(dest_parent.glob("cursor-handoff.bak.*")))

        import install as install_mod

        dest2_parent = self.tmp / "skills2"
        dest2 = dest2_parent / "cursor-handoff"
        dest2.mkdir(parents=True)
        (dest2 / "SKILL.md").write_text("keep-me\n", encoding="utf-8")

        def boom(src: Path, dest_path: Path) -> None:
            raise RuntimeError("simulated copy failure")

        original = install_mod.copy_skill
        install_mod.copy_skill = boom  # type: ignore[assignment]
        try:
            with self.assertRaises(Exception):
                install_mod.install_one(ROOT / "skills" / "cursor-handoff", dest2, update=True)
        finally:
            install_mod.copy_skill = original  # type: ignore[assignment]
        self.assertTrue(dest2.is_dir())
        self.assertEqual((dest2 / "SKILL.md").read_text(encoding="utf-8"), "keep-me\n")

        proc2 = run_py(
            [
                str(INSTALL),
                "--target",
                "both",
                "--scope",
                "project",
                "--project",
                str(self.tmp / "proj"),
                "--destination",
                str(self.tmp / "x"),
            ]
        )
        self.assertNotEqual(proc2.returncode, 0)

    def test_broken_symlink_destination_rejected(self) -> None:
        dest_parent = self.tmp / "skills"
        dest_parent.mkdir(parents=True)
        dest = dest_parent / "cursor-handoff"
        missing = self.tmp / "missing-target"
        try:
            dest.symlink_to(missing, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable")
        proc = run_py(
            [
                str(INSTALL),
                "--target",
                "codex",
                "--scope",
                "user",
                "--destination",
                str(dest_parent),
                "--update",
            ]
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("symlink", (proc.stdout + proc.stderr).lower())

    def test_symlink_parent_rejected(self) -> None:
        real_parent = self.tmp / "real-skills"
        real_parent.mkdir()
        link_parent = self.tmp / "link-skills"
        try:
            link_parent.symlink_to(real_parent, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable")
        proc = run_py(
            [
                str(INSTALL),
                "--target",
                "codex",
                "--scope",
                "user",
                "--destination",
                str(link_parent),
            ]
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("symlink", (proc.stdout + proc.stderr).lower())


class PackageTests(unittest.TestCase):
    def test_package_excludes_private_and_reproducible(self) -> None:
        (ROOT / ".cursor-handoff").mkdir(exist_ok=True)
        (ROOT / ".handoff-dev").mkdir(exist_ok=True)
        marker = ROOT / ".handoff-dev" / "secret-should-not-pack.txt"
        marker.write_text("secret\n", encoding="utf-8")
        try:
            proc1 = run_py([str(PACKAGE)], cwd=ROOT)
            self.assertEqual(proc1.returncode, 0, proc1.stdout + proc1.stderr)
            version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
            zip_path = ROOT / "dist" / f"cursor-handoff-{version}.zip"
            self.assertTrue(zip_path.is_file())
            with zipfile.ZipFile(zip_path) as zf:
                names = set(zf.namelist())
                for info in zf.infolist():
                    self.assertEqual(info.create_system, 0)
            self.assertIn("install.py", names)
            self.assertIn("skills/cursor-handoff/scripts/handoff.py", names)
            self.assertIn("skills/cursor-handoff/scripts/consult.py", names)
            self.assertIn("skills/cursor-handoff/scripts/progress.py", names)
            self.assertIn("skills/cursor-handoff/scripts/watch.py", names)
            self.assertIn("examples/design-brief.md", names)
            self.assertNotIn(".handoff-dev/secret-should-not-pack.txt", names)
            self.assertTrue(all(not n.startswith(".cursor-handoff/") for n in names))
            digest1 = hashlib.sha256(zip_path.read_bytes()).hexdigest()

            proc2 = run_py([str(PACKAGE)], cwd=ROOT)
            self.assertEqual(proc2.returncode, 0, proc2.stdout + proc2.stderr)
            digest2 = hashlib.sha256(zip_path.read_bytes()).hexdigest()
            self.assertEqual(digest1, digest2)
            sha_file = ROOT / "dist" / f"cursor-handoff-{version}.zip.sha256"
            self.assertIn(digest1, sha_file.read_text(encoding="utf-8"))
        finally:
            if marker.exists():
                marker.unlink()

    def test_version_path_escape_rejected(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("package_release", PACKAGE)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        original = mod.VERSION_FILE.read_text(encoding="utf-8")
        try:
            mod.VERSION_FILE.write_text("../evil\n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                mod.read_version()
            mod.VERSION_FILE.write_text("not-a-semver\n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                mod.read_version()
        finally:
            mod.VERSION_FILE.write_text(original, encoding="utf-8")

    def test_ancestor_symlink_rejected(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("package_release", PACKAGE)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            real = tmp_path / "real"
            real.mkdir()
            (real / "file.txt").write_text("x\n", encoding="utf-8")
            link = tmp_path / "linked"
            try:
                link.symlink_to(real, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation unavailable")
            target = link / "file.txt"
            self.assertIsNotNone(mod.path_has_symlink_ancestor(target))
            original_root = mod.ROOT
            original_allow = list(mod.ALLOWLIST)
            mod.ROOT = tmp_path
            mod.ALLOWLIST = ["linked/file.txt"]
            try:
                with self.assertRaises(SystemExit) as ctx:
                    mod.collect_files()
                self.assertIn("symlink", str(ctx.exception).lower())
            finally:
                mod.ROOT = original_root
                mod.ALLOWLIST = original_allow


if __name__ == "__main__":
    unittest.main()
