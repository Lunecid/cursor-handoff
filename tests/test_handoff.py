#!/usr/bin/env python3
"""Integration tests for cursor-handoff (fake agent only; no live API)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
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
        "#!/usr/bin/env python3\n"
        "import os, runpy\n"
        "runpy.run_path(os.environ['FAKE_AGENT_PY'], run_name='__main__')\n",
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
