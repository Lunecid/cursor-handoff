#!/usr/bin/env python3
"""Fake Claude Code CLI for offline consult tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time

HELP = """
Usage: fake-claude [options] <prompt>

Options:
  --version
  --help
  -p, --print
  --output-format <fmt>
  --tools <list>
  --disable-slash-commands
  --strict-mcp-config
  --mcp-config <file>
  --no-session-persistence
  --setting-sources <sources>
  --settings <file>
  --model <id>
"""

HELP_MINIMAL = """
Usage: fake-claude
Options:
  --version
  --help
"""


def emit_result(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.stdout.flush()


def parse_args(argv: list[str]) -> dict:
    info: dict = {
        "argv": list(argv),
        "tools": None,
        "tools_present": False,
        "output_format": None,
        "mcp_config": None,
        "settings": None,
        "setting_sources": None,
        "has_p": False,
        "disable_slash": False,
        "strict_mcp": False,
        "no_session": False,
        "model": None,
        "prompt": None,
        "dangerous": [],
    }
    i = 0
    positional: list[str] = []
    while i < len(argv):
        arg = argv[i]
        if arg in {"-p", "--print"}:
            info["has_p"] = True
            i += 1
            continue
        if arg == "--output-format":
            info["output_format"] = argv[i + 1] if i + 1 < len(argv) else None
            i += 2
            continue
        if arg == "--tools":
            info["tools_present"] = True
            info["tools"] = argv[i + 1] if i + 1 < len(argv) else None
            i += 2
            continue
        if arg == "--disable-slash-commands":
            info["disable_slash"] = True
            i += 1
            continue
        if arg == "--strict-mcp-config":
            info["strict_mcp"] = True
            i += 1
            continue
        if arg == "--mcp-config":
            info["mcp_config"] = argv[i + 1] if i + 1 < len(argv) else None
            i += 2
            continue
        if arg == "--no-session-persistence":
            info["no_session"] = True
            i += 1
            continue
        if arg == "--setting-sources":
            info["setting_sources"] = argv[i + 1] if i + 1 < len(argv) else None
            i += 2
            continue
        if arg == "--settings":
            info["settings"] = argv[i + 1] if i + 1 < len(argv) else None
            i += 2
            continue
        if arg == "--model":
            info["model"] = argv[i + 1] if i + 1 < len(argv) else None
            i += 2
            continue
        if arg in {"--bare", "--dangerously-skip-permissions", "--dangerously-skip-permissions=true"}:
            info["dangerous"].append(arg)
            i += 1
            continue
        if arg.startswith("-"):
            # Unknown flag: keep scanning
            i += 1
            continue
        positional.append(arg)
        i += 1
    info["prompt"] = positional[-1] if positional else None
    return info


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    mode = os.environ.get("FAKE_CLAUDE_MODE", "success")

    if "--version" in argv:
        print("fake-claude 0.0.1")
        return 0
    if "--help" in argv or "-h" in argv:
        if mode == "help_incomplete":
            print(HELP_MINIMAL)
            return 0
        if mode == "help_fail":
            sys.stderr.write("help unavailable\n")
            return 2
        print(HELP)
        return 0

    info = parse_args(argv)
    stdin_data = sys.stdin.buffer.read()
    record = os.environ.get("FAKE_CLAUDE_RECORD")
    if record:
        payload = {
            "argv": info["argv"],
            "parsed": {k: v for k, v in info.items() if k != "argv"},
            "stdin_bytes": list(stdin_data),
            "stdin_text": stdin_data.decode("utf-8", errors="replace"),
            "cwd": os.getcwd(),
        }
        Path(record).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    sleep_s = float(os.environ.get("FAKE_CLAUDE_SLEEP", "0"))
    if sleep_s > 0:
        time.sleep(sleep_s)

    if mode == "nonzero":
        sys.stderr.write("fake claude failed\n")
        return 7

    if mode == "missing":
        return 0

    if mode == "malformed":
        sys.stdout.write("not-json{{{")
        return 0

    if mode == "quota":
        # Synthetic quota: subtype success with is_error true and nonzero exit.
        emit_result(
            {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "api_error_status": 429,
                "result": "weekly limit reached",
                "session_id": "sess-quota",
            }
        )
        return 1

    if mode == "is_error":
        emit_result(
            {
                "type": "result",
                "subtype": "error",
                "is_error": True,
                "result": "simulated provider error",
                "session_id": "sess-err",
            }
        )
        return 0

    if mode == "success_subtype_only":
        # Missing is_error=false must not count as success.
        emit_result(
            {
                "type": "result",
                "subtype": "success",
                "session_id": "sess-bad",
                "result": "should fail",
            }
        )
        return 0

    # Default success
    emit_result(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "session_id": "sess-advice",
            "result": (
                "## Critique\n"
                "- Prefer keeping Cursor as sole executor.\n"
                "- Unresolved: timeout defaults for consult.\n"
            ),
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
