#!/usr/bin/env python3
"""Offline tests for consult.py (fake Claude only; no live API)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CONSULT = ROOT / "skills" / "cursor-handoff" / "scripts" / "consult.py"
FAKE_CLAUDE = Path(__file__).resolve().parent / "fake_claude.py"
BRIEF_MAX = 128 * 1024


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


def make_claude_launcher(dir_path: Path) -> Path:
    """Create a portable fake Claude path suitable for --claude-path."""
    dir_path.mkdir(parents=True, exist_ok=True)
    script = dir_path / "claude.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os, runpy\n"
        "runpy.run_path(os.environ['FAKE_CLAUDE_PY'], run_name='__main__')\n",
        encoding="utf-8",
        newline="\n",
    )
    if os.name != "nt":
        script.chmod(0o755)
    return script


class ConsultTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        # Canonical root so macOS /var -> /private/var is not treated as a fixture symlink.
        self.tmp = Path(self._tmpdir.name).resolve()
        self.workspace = self.tmp / "workspace"
        self.workspace.mkdir()
        self.claude_dir = self.tmp / "claude"
        self.claude = make_claude_launcher(self.claude_dir)
        self.base_env = {
            "FAKE_CLAUDE_PY": str(FAKE_CLAUDE),
            "FAKE_CLAUDE_PYTHON": sys.executable,
            "FAKE_CLAUDE_MODE": "success",
            "FAKE_CLAUDE_SLEEP": "0",
        }

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def write_brief(
        self,
        name: str = "brief.md",
        body: str = "# Design brief\n\nObjective: ship consult.\n",
    ) -> Path:
        path = self.workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8", newline="\n")
        return path

    def run_consult(
        self,
        brief: Path,
        extra: list[str] | None = None,
        env: dict | None = None,
        timeout: float = 60.0,
        workspace: Path | None = None,
    ):
        ws = workspace or self.workspace
        args = [
            str(CONSULT),
            "--workspace",
            str(ws),
            "--brief",
            str(brief),
            "--claude-path",
            str(self.claude),
            "--timeout",
            "30",
        ]
        if extra:
            args.extend(extra)
        merged = dict(self.base_env)
        if env:
            merged.update(env)
        return run_py(args, env=merged, timeout=timeout)

    def latest_run(self, workspace: Path | None = None) -> Path:
        ws = workspace or self.workspace
        runs = sorted((ws / ".cursor-handoff").glob("consult-*"))
        runs = [p for p in runs if p.is_dir()]
        self.assertTrue(runs, "expected a consult run directory")
        return runs[-1]

    def latest_status(self, workspace: Path | None = None) -> dict:
        return json.loads((self.latest_run(workspace) / "status.json").read_text(encoding="utf-8"))

    def test_success_advice_received(self) -> None:
        brief = self.write_brief()
        proc = self.run_consult(brief)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        status = self.latest_status()
        self.assertEqual(status["state"], "advice_received")
        self.assertFalse(status.get("agent_is_error", True))
        advice = (self.latest_run() / "advice.md").read_text(encoding="utf-8")
        self.assertIn("Critique", advice)
        self.assertFalse((self.workspace / ".cursor-handoff" / "workspace.lock").exists())

    def test_stdin_preserves_unicode_and_shell_chars(self) -> None:
        body = (
            "# Brief\n\n"
            "Unicode: 한글 café\n"
            "Shell-like: `rm -rf /` && echo $(whoami); | cat > /tmp/x\n"
            "Quotes: \"double\" 'single'\n"
        )
        brief = self.write_brief(body=body)
        record = self.tmp / "claude-record.json"
        proc = self.run_consult(brief, env={"FAKE_CLAUDE_RECORD": str(record)})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(record.read_text(encoding="utf-8"))
        self.assertEqual(payload["stdin_text"], body)
        self.assertIn("`rm -rf /`", payload["stdin_text"])
        parsed = payload["parsed"]
        self.assertTrue(parsed["tools_present"])
        self.assertEqual(parsed["tools"], "")
        self.assertTrue(parsed["disable_slash"])
        self.assertTrue(parsed["strict_mcp"])
        self.assertTrue(parsed["no_session"])
        self.assertEqual(parsed["setting_sources"], "")
        self.assertEqual(parsed["dangerous"], [])
        cwd = Path(payload["cwd"]).resolve()
        ws = self.workspace.resolve()
        with self.assertRaises(ValueError):
            cwd.relative_to(ws)
        self.assertNotEqual(cwd, ws)
        self.assertIn("cursor-handoff-consult-", cwd.name)
        settings = json.loads(
            (self.latest_run() / "settings.disable-hooks.json").read_text(encoding="utf-8")
        )
        self.assertEqual(settings.get("disableAllHooks"), True)
        mcp = (self.latest_run() / "mcp.empty.json").read_text(encoding="utf-8").strip()
        self.assertEqual(mcp, "{}")

    def test_dispatch_cwd_outside_workspace_leaves_project_untouched(self) -> None:
        """Claude process cwd must not sit under the workspace (avoids ancestor CLAUDE.md)."""
        sentinel = self.workspace / "CLAUDE.md"
        sentinel_body = "# project CLAUDE.md SENTINEL_UNTOUCHED\n"
        sentinel.write_text(sentinel_body, encoding="utf-8", newline="\n")
        source = self.workspace / "keep_me.py"
        source_body = "KEEP = True\n"
        source.write_text(source_body, encoding="utf-8", newline="\n")
        brief = self.write_brief()
        record = self.tmp / "cwd-record.json"
        proc = self.run_consult(brief, env={"FAKE_CLAUDE_RECORD": str(record)})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(record.read_text(encoding="utf-8"))
        cwd = Path(payload["cwd"]).resolve()
        ws = self.workspace.resolve()
        with self.assertRaises(ValueError):
            cwd.relative_to(ws)
        self.assertFalse((ws / ".cursor-handoff").joinpath("consult-dummy", "empty-cwd").exists())
        status = self.latest_status()
        self.assertEqual(Path(status["consult_cwd"]).resolve(), cwd)
        # Project sentinel and sources must remain untouched.
        self.assertEqual(sentinel.read_text(encoding="utf-8"), sentinel_body)
        self.assertEqual(source.read_text(encoding="utf-8"), source_body)
        # Ephemeral cwd is cleaned up after the process; only owned temp is removed.
        self.assertFalse(cwd.exists())
        # Tools restrictions still enforced.
        self.assertTrue(payload["parsed"]["tools_present"])
        self.assertEqual(payload["parsed"]["tools"], "")
        self.assertEqual(payload["parsed"]["dangerous"], [])

    def test_quota_is_error_not_advice(self) -> None:
        brief = self.write_brief()
        proc = self.run_consult(brief, env={"FAKE_CLAUDE_MODE": "quota"})
        self.assertNotEqual(proc.returncode, 0)
        status = self.latest_status()
        self.assertEqual(status["state"], "failed")
        self.assertTrue(status.get("agent_is_error"))
        self.assertIn("is_error", status.get("error", "").lower())
        self.assertEqual(status.get("blocker"), "quota_or_provider_error")

    def test_is_error_result(self) -> None:
        brief = self.write_brief()
        proc = self.run_consult(brief, env={"FAKE_CLAUDE_MODE": "is_error"})
        self.assertNotEqual(proc.returncode, 0)
        status = self.latest_status()
        self.assertEqual(status["state"], "failed")

    def test_subtype_success_without_is_error_false(self) -> None:
        brief = self.write_brief()
        proc = self.run_consult(brief, env={"FAKE_CLAUDE_MODE": "success_subtype_only"})
        self.assertNotEqual(proc.returncode, 0)
        status = self.latest_status()
        self.assertEqual(status["state"], "failed")

    def test_nonzero_exit(self) -> None:
        brief = self.write_brief()
        proc = self.run_consult(brief, env={"FAKE_CLAUDE_MODE": "nonzero"})
        self.assertNotEqual(proc.returncode, 0)
        status = self.latest_status()
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status.get("exit_code"), 7)

    def test_missing_response(self) -> None:
        brief = self.write_brief()
        proc = self.run_consult(brief, env={"FAKE_CLAUDE_MODE": "missing"})
        self.assertNotEqual(proc.returncode, 0)
        status = self.latest_status()
        self.assertEqual(status["state"], "failed")
        self.assertIn("Missing JSON", status.get("error", ""))

    def test_malformed_response(self) -> None:
        brief = self.write_brief()
        proc = self.run_consult(brief, env={"FAKE_CLAUDE_MODE": "malformed"})
        self.assertNotEqual(proc.returncode, 0)
        status = self.latest_status()
        self.assertEqual(status["state"], "failed")
        self.assertIn("Malformed", status.get("error", ""))

    def test_timeout_precedence(self) -> None:
        brief = self.write_brief()
        proc = self.run_consult(
            brief,
            extra=["--timeout", "1"],
            env={"FAKE_CLAUDE_MODE": "success", "FAKE_CLAUDE_SLEEP": "5"},
            timeout=30.0,
        )
        self.assertNotEqual(proc.returncode, 0)
        status = self.latest_status()
        self.assertEqual(status["state"], "timeout")

    def test_dry_run_no_artifacts(self) -> None:
        brief = self.write_brief()
        before = list(self.workspace.rglob("*"))
        proc = self.run_consult(brief, extra=["--dry-run"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        after = list(self.workspace.rglob("*"))
        self.assertEqual(before, after)
        self.assertNotIn("Objective: ship consult", proc.stdout)
        self.assertIn("dry-run", proc.stdout)

    def test_missing_claude_path(self) -> None:
        missing = self.claude_dir / "missing-claude"
        brief = self.write_brief()
        env = dict(self.base_env)
        env["CLAUDE_CODE_PATH"] = str(self.claude)
        proc = run_py(
            [
                str(CONSULT),
                "--workspace",
                str(self.workspace),
                "--brief",
                str(brief),
                "--claude-path",
                str(missing),
            ],
            env=env,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("not found", (proc.stdout + proc.stderr).lower())

    def test_reject_cmd_override(self) -> None:
        bad = self.claude_dir / "claude.cmd"
        bad.write_text("@echo off\n", encoding="utf-8")
        brief = self.write_brief()
        env = dict(self.base_env)
        env["CLAUDE_CODE_PATH"] = str(self.claude)
        proc = run_py(
            [
                str(CONSULT),
                "--workspace",
                str(self.workspace),
                "--brief",
                str(brief),
                "--claude-path",
                str(bad),
            ],
            env=env,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("unsafe", (proc.stdout + proc.stderr).lower())

    def test_brief_escape_rejected(self) -> None:
        outside = self.tmp / "outside.md"
        outside.write_text("# nope\n", encoding="utf-8")
        proc = self.run_consult(outside)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("inside the workspace", (proc.stdout + proc.stderr).lower())

    def test_brief_size_limit(self) -> None:
        huge = "x" * (BRIEF_MAX + 1)
        brief = self.write_brief(body=huge)
        proc = self.run_consult(brief)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("size limit", (proc.stdout + proc.stderr).lower())

    def test_probe_incomplete_help_no_dispatch(self) -> None:
        brief = self.write_brief()
        proc = self.run_consult(brief, env={"FAKE_CLAUDE_MODE": "help_incomplete"})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("required flags", (proc.stdout + proc.stderr).lower())
        handoff = self.workspace / ".cursor-handoff"
        if handoff.exists():
            runs = [p for p in handoff.iterdir() if p.is_dir()]
            self.assertEqual(runs, [], "probe failure must not create a consult directory")

    def test_doctor_help_fail(self) -> None:
        proc = run_py(
            [str(CONSULT), "--doctor", "--claude-path", str(self.claude)],
            env={**self.base_env, "FAKE_CLAUDE_MODE": "help_fail"},
        )
        self.assertNotEqual(proc.returncode, 0)
        report = json.loads(proc.stdout)
        self.assertFalse(report.get("ok", True))
        blob = json.dumps(report).lower()
        self.assertNotRegex(blob, r"account[_-]?id")
        self.assertNotIn("@", blob)

    def test_unicode_workspace_legacy_stdout(self) -> None:
        """Workspace with non-ASCII name must not crash under legacy stdout encoding."""
        ws = self.tmp / "작업공간"
        ws.mkdir()
        brief = ws / "brief.md"
        brief.write_text("# 설계\n\n옵션 A\n", encoding="utf-8", newline="\n")
        env = dict(self.base_env)
        env["PYTHONIOENCODING"] = "cp949"
        proc = run_py(
            [
                str(CONSULT),
                "--workspace",
                str(ws),
                "--brief",
                str(brief),
                "--claude-path",
                str(self.claude),
                "--timeout",
                "30",
            ],
            env=env,
            timeout=60.0,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        status_line = [ln for ln in proc.stdout.splitlines() if "advice_received" in ln][-1]
        parsed = json.loads(status_line)
        self.assertEqual(parsed["state"], "advice_received")


if __name__ == "__main__":
    unittest.main()
