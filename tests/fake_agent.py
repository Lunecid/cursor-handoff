#!/usr/bin/env python3
"""Fake Cursor agent for offline handoff tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

HELP = """
Usage: fake-agent [options] <prompt>

Options:
  --version
  --help
  --workspace <path>
  --print
  --output-format <fmt>
  --auto-review
  --trust
  --model <id>
"""

HELP_MINIMAL = """
Usage: fake-agent
Options:
  --version
  --help
"""

TASK_RE = re.compile(r"Read the UTF-8 task file at (.+?)\. Complete")


def emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def record_prompt_task(argv: list[str]) -> None:
    record = os.environ.get("FAKE_AGENT_RECORD")
    if not record:
        return
    prompt = argv[-1] if argv else ""
    match = TASK_RE.search(prompt)
    payload = {
        "argv": argv,
        "prompt": prompt,
        "task_path": match.group(1) if match else None,
    }
    Path(record).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def spawn_descendant() -> None:
    """Spawn a long-lived child in the same process tree/group as this agent."""
    marker = os.environ.get("FAKE_AGENT_CHILD_MARKER")
    if not marker:
        return
    child_sleep = float(os.environ.get("FAKE_AGENT_CHILD_SLEEP", "30"))
    # Stay in the parent process group/tree so runner cleanup (killpg / taskkill /T)
    # is responsible for stopping this descendant.
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import os, time, pathlib;"
                f"pathlib.Path(r'''{marker}''').write_text(str(os.getpid()));"
                f"time.sleep({child_sleep})"
            ),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    for _ in range(50):
        if Path(marker).exists():
            break
        time.sleep(0.05)
    _ = proc


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    mode = os.environ.get("FAKE_AGENT_MODE", "success")

    if "--version" in argv:
        print("fake-agent 0.0.1")
        return 0
    if "--help" in argv or "-h" in argv:
        if mode == "help_incomplete":
            print(HELP_MINIMAL)
            return 0
        if mode == "help_fail":
            sys.stderr.write("help unavailable\n")
            return 2
        if mode == "help_timeout":
            time.sleep(float(os.environ.get("FAKE_AGENT_HELP_SLEEP", "30")))
            print(HELP)
            return 0
        print(HELP)
        return 0

    record_prompt_task(argv)

    sleep_s = float(os.environ.get("FAKE_AGENT_SLEEP", "0"))
    if mode == "child_hang":
        spawn_descendant()
        if sleep_s <= 0:
            sleep_s = float(os.environ.get("FAKE_AGENT_CHILD_SLEEP", "30"))
        time.sleep(sleep_s)
        return 0

    if sleep_s > 0:
        time.sleep(sleep_s)

    if mode == "nonzero":
        sys.stderr.write("fake agent failed\n")
        return 7

    if mode == "trust_denied":
        sys.stderr.write(
            "Error: Workspace Trust is required. User declined workspace trust.\n"
        )
        return 2

    if mode == "unrelated_stderr":
        sys.stderr.write("FAIL: test_trust_helper assertion failed\n")
        emit(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "session_id": "sess-unrelated",
                "result": "ok",
            }
        )
        return 0

    if mode == "progress_events":
        emit({"type": "system", "subtype": "init", "session_id": "sess-progress"})
        emit({"type": "thinking", "text": "secret chain of thought - must not render"})
        emit(
            {
                "type": "reasoning",
                "summary": "also must not render",
            }
        )
        emit(
            {
                "type": "tool_call",
                "subtype": "started",
                "session_id": "sess-progress",
                "tool_call": {"shellToolCall": {"args": {"command": "secret payload"}}},
            }
        )
        time.sleep(float(os.environ.get("FAKE_AGENT_PROGRESS_SLEEP", "0.4")))
        emit(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "session_id": "sess-progress",
                "result": "done",
            }
        )
        return 0

    if mode == "missing":
        emit({"type": "system", "message": "no result will be sent"})
        return 0

    if mode == "error":
        emit(
            {
                "type": "result",
                "subtype": "error",
                "is_error": True,
                "session_id": "sess-error",
                "result": "simulated error",
            }
        )
        return 0

    if mode == "malformed_result":
        emit({"type": "result", "is_error": False, "session_id": "sess-bad"})
        return 0

    if mode == "array_event":
        emit(["not", "an", "object"])
        emit(None)  # type: ignore[arg-type]
        emit(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "session_id": "sess-after-array",
                "result": "ok",
            }
        )
        return 0

    if mode == "error_then_success":
        emit(
            {
                "type": "result",
                "subtype": "error",
                "is_error": True,
                "session_id": "sess-first-error",
                "result": "first",
            }
        )
        emit(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "session_id": "sess-second-success",
                "result": "second",
            }
        )
        return 0

    if mode == "late_result_after_sleep":
        # Emit success after a long sleep so timeout can race the reader.
        emit(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "session_id": "sess-late",
                "result": "late",
            }
        )
        return 0

    # success (default)
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}})
    emit(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "session_id": "sess-ok",
            "result": "done",
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
