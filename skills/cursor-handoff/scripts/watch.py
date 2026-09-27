#!/usr/bin/env python3
"""Observe a cursor-handoff run directory. Observation only — cannot answer trust prompts."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# Allow running as a script from any cwd.
_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from progress import (  # noqa: E402
    ProgressReporter,
    TERMINAL_STATES,
    iter_new_jsonl_events,
    read_json_object,
    read_new_text,
)


def configure_stdio_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError, AttributeError):
                pass


def attach_windows_console() -> dict:
    """Reopen stdio onto this process console (CONIN$/CONOUT$).

    Needed when the parent launched us with CREATE_NEW_CONSOLE but inherited
    piped/DEVNULL handles, so the visible console would otherwise stay blank.
    """
    info: dict = {
        "attached": False,
        "stdin_isatty": False,
        "stdout_isatty": False,
    }
    if os.name != "nt":
        info["note"] = "console attach is Windows-only"
        return info
    try:
        # Binary-safe reopen onto the new console buffers.
        sys.stdin = open("CONIN$", "r", encoding="utf-8", errors="replace", buffering=1)
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
        sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
        info["attached"] = True
    except OSError as exc:
        info["error"] = str(exc)
        return info
    info["stdin_isatty"] = bool(getattr(sys.stdin, "isatty", lambda: False)())
    info["stdout_isatty"] = bool(getattr(sys.stdout, "isatty", lambda: False)())
    return info


def write_console_handshake(run_dir: Path, console_info: dict, *, content: str) -> None:
    """Observable proof for tests: isatty + content reached the viewer console path."""
    payload = {
        **console_info,
        "content": content,
        "pid": os.getpid(),
    }
    path = run_dir / "viewer-console.json"
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def status_announcement_key(status: dict) -> tuple:
    """Fingerprint status details so detail-only updates still get announced."""
    return (
        status.get("state"),
        status.get("block_reason"),
        status.get("error"),
        status.get("next_action"),
        status.get("exit_code"),
        status.get("session_id"),
        status.get("finished_at"),
        status.get("pid"),
        status.get("workspace"),
        status.get("trust_workspace"),
    )


def follow_run(run_dir: Path, *, poll_s: float = 0.35, stream=None) -> int:
    """Incrementally follow run artifacts until a finalized terminal status."""
    reporter = ProgressReporter(stream or sys.stdout, prefix_enabled=True)
    run_dir = run_dir.expanduser()
    try:
        run_dir = run_dir.resolve()
    except OSError:
        run_dir = run_dir.absolute()

    reporter.emit_line(f"watching run: {run_dir}")
    reporter.emit_line(
        "note: viewer is observation only and cannot answer Cursor trust prompts"
    )

    task_shown = False
    meta_shown = False
    events_offset = 0
    stderr_offset = 0
    last_status_key: tuple | None = None
    last_pid: object | None = None
    idle_after_final = 0

    while True:
        task_path = run_dir / "task.md"
        if not task_shown and task_path.is_file():
            reporter.show_task_snapshot(task_path)
            task_shown = True

        events_path = run_dir / "events.jsonl"
        if events_path.exists():
            events, events_offset = iter_new_jsonl_events(events_path, events_offset)
            for event in events:
                reporter.on_event(event)

        stderr_path = run_dir / "stderr.log"
        if stderr_path.exists():
            chunk, stderr_offset = read_new_text(stderr_path, stderr_offset)
            if chunk:
                reporter.on_stderr_text(chunk)

        status_path = run_dir / "status.json"
        status = read_json_object(status_path) if status_path.exists() else None
        if status:
            if not meta_shown:
                workspace = status.get("workspace")
                if workspace:
                    reporter.emit_line(f"workspace: {workspace}")
                if "trust_workspace" in status:
                    reporter.emit_line(f"trust_workspace: {bool(status.get('trust_workspace'))}")
                meta_shown = True

            pid = status.get("pid")
            if pid is not None and pid != last_pid:
                reporter.announce_pid(pid)
                last_pid = pid

            key = status_announcement_key(status)
            if key != last_status_key:
                reporter.on_status(status)
                last_status_key = key

            state = status.get("state")
            # Only finish on a finalized terminal status (finished_at set).
            # Transient failed/running writes before finalization must not exit early
            # and miss a later trust_required block.
            if (
                isinstance(state, str)
                and state in TERMINAL_STATES
                and status.get("finished_at")
            ):
                idle_after_final += 1
                if idle_after_final >= 2:
                    return 0
            else:
                idle_after_final = 0
        elif not run_dir.exists():
            reporter.emit_line("run directory missing")
            return 1

        time.sleep(max(0.1, poll_s))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Follow cursor-handoff run artifacts (observation only; "
            "cannot answer Cursor trust prompts)."
        )
    )
    parser.add_argument(
        "--run",
        type=Path,
        required=True,
        help="Path to .cursor-handoff/<run-id>/ directory",
    )
    parser.add_argument(
        "--poll",
        type=float,
        default=0.35,
        help="Polling interval seconds (default 0.35; no busy loop)",
    )
    parser.add_argument(
        "--console",
        action="store_true",
        help=(
            "Windows: reopen stdin/stdout/stderr onto CONIN$/CONOUT$ so a "
            "CREATE_NEW_CONSOLE viewer stays interactive even when the parent "
            "had piped or null std handles."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    console_info: dict = {"attached": False}
    if args.console:
        console_info = attach_windows_console()
    configure_stdio_utf8()
    if args.console:
        handshake_line = "viewer console ready"
        try:
            print(f"[handoff] {handshake_line}", file=sys.stdout, flush=True)
        except Exception:
            pass
        write_console_handshake(args.run, console_info, content=handshake_line)
    code = follow_run(args.run, poll_s=args.poll)
    hold = os.environ.get("CURSOR_HANDOFF_VIEWER_NO_HOLD", "").strip().lower() not in {
        "1",
        "true",
        "yes",
    }
    if hold and sys.stdin is not None and sys.stdin.isatty():
        try:
            input("Press Enter to close viewer...")
        except EOFError:
            pass
    return code


if __name__ == "__main__":
    raise SystemExit(main())
