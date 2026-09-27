#!/usr/bin/env python3
"""Shared progress rendering for handoff --live and watch.py.

Observation only: never prints reasoning or full tool payloads.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import TextIO

# Strip C0 controls (except tab/LF/CR, normalized away separately) and C1 CSI/OSC range.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_REASONING_TYPES = frozenset({"thinking", "reasoning", "thought"})
TERMINAL_STATES = frozenset(
    {
        "execution_completed",
        "failed",
        "timeout",
        "interrupted",
        "runner_error",
        "blocked",
    }
)

# Explicit workspace-trust denial only — not ordinary test/assertion stderr.
TRUST_DENIAL_RE = re.compile(
    r"(?:"
    r"workspace\s+trust\s+(?:is\s+)?(?:required|denied|rejected|refused|not\s+(?:granted|accepted))"
    r"|user\s+(?:denied|rejected|declined)\s+(?:the\s+)?(?:workspace\s+)?trust"
    r"|(?:denied|rejected|refused)\s+(?:the\s+)?workspace\s+trust"
    r"|must\s+trust\s+(?:this\s+)?workspace"
    r"|trust\s+(?:this\s+)?workspace\s+(?:to\s+continue|before|first)"
    r"|trust\s+(?:prompt|request|dialog)\s+(?:was\s+)?(?:denied|rejected|declined)"
    r"|workspace\s+is\s+not\s+trusted"
    r")",
    re.IGNORECASE,
)


def strip_control_chars(text: str) -> str:
    """Remove unsafe terminal control characters; keep plain readable text."""
    cleaned = _CONTROL_RE.sub("", text)
    return cleaned.replace("\r", "")


def detect_trust_denial(stderr_text: str) -> bool:
    """True only for explicit workspace-trust denial/requirement phrasing."""
    if not stderr_text:
        return False
    return bool(TRUST_DENIAL_RE.search(stderr_text))


def trust_block_fields() -> dict:
    return {
        "state": "blocked",
        "block_reason": "trust_required",
        "error": (
            "Cursor reported an explicit workspace-trust denial or requirement. "
            "The runner did not auto-enable --trust."
        ),
        "next_action": (
            "If you already declined/rejected workspace trust for this folder, "
            "do not bypass that consent decision. "
            "If trust was never configured, deliberately pass --trust-workspace "
            "for this exact workspace (Cursor may write a persistent "
            ".workspace-trusted marker that subdirectories can inherit) "
            "or complete interactive Cursor workspace trust setup. "
            "Never auto-enable trust; viewer windows cannot answer trust prompts."
        ),
    }


def _tool_name_from_call_object(tool_call: object) -> str | None:
    if not isinstance(tool_call, dict):
        return None
    if "name" in tool_call and isinstance(tool_call["name"], str):
        return tool_call["name"]
    for key in tool_call:
        if key.endswith("ToolCall") and isinstance(tool_call[key], dict):
            return key[: -len("ToolCall")] or key
        if key.endswith("Tool") and key != "tool":
            return key
    keys = [k for k in tool_call.keys() if k not in {"args", "arguments", "id", "call_id"}]
    if len(keys) == 1:
        return keys[0]
    return None


def _nested_call_id(tool_call: object) -> str | None:
    if not isinstance(tool_call, dict):
        return None
    for key in ("call_id", "id"):
        value = tool_call.get(key)
        if isinstance(value, str) and value:
            return value
    for value in tool_call.values():
        if isinstance(value, dict):
            for key in ("call_id", "id"):
                nested = value.get(key)
                if isinstance(nested, str) and nested:
                    return nested
    return None


def _event_dedupe_key(event: dict) -> str | None:
    """Stable id+subtype key, or None when the event has no call/event id (always emit)."""
    etype = event.get("type")
    subtype = event.get("subtype")
    call_id = event.get("call_id") or event.get("id")
    if not isinstance(call_id, str) or not call_id:
        call_id = _nested_call_id(event.get("tool_call"))
    if not isinstance(call_id, str) or not call_id:
        return None
    return f"{etype}|{subtype}|{call_id}"


def extract_tool_names(event: dict) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()

    def add(name: object) -> None:
        if isinstance(name, str) and name and name not in seen:
            seen.add(name)
            names.append(name)

    add(event.get("name"))
    add(event.get("tool"))
    nested = event.get("tool_call")
    add(_tool_name_from_call_object(nested))
    if isinstance(nested, dict):
        add(nested.get("name"))

    message = event.get("message")
    content = None
    if isinstance(message, dict):
        content = message.get("content")
    elif isinstance(event.get("content"), list):
        content = event.get("content")
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype in {"tool_use", "tool-call", "tool_call"}:
                add(part.get("name"))
            elif ptype and str(ptype).endswith("ToolCall"):
                add(str(ptype)[: -len("ToolCall")])
    return names


def summarize_event(event: dict) -> str | None:
    """One-line human summary, or None to skip (reasoning/noise/full payloads)."""
    if not isinstance(event, dict):
        return None
    etype = event.get("type")
    if not isinstance(etype, str):
        return None
    lower = etype.lower()
    if lower in _REASONING_TYPES or "thinking" in lower or "reasoning" in lower:
        return None

    if etype == "system":
        session = event.get("session_id") or event.get("sessionId")
        subtype = event.get("subtype")
        bits = ["system"]
        if subtype:
            bits.append(str(subtype))
        if session:
            bits.append(f"session={session}")
        return " ".join(bits)

    if etype in {"tool_call", "tool-call", "tool_use", "tool-use"}:
        names = extract_tool_names(event)
        subtype = event.get("subtype")
        label = ", ".join(names) if names else "tool"
        if subtype:
            return f"tool {label} ({subtype})"
        return f"tool {label}"

    if etype in {"tool_result", "tool-result"}:
        names = extract_tool_names(event)
        if names:
            return f"tool result {', '.join(names)}"
        return "tool result"

    if etype == "assistant":
        names = extract_tool_names(event)
        if names:
            return f"assistant tool {', '.join(names)}"
        return None

    if etype == "result":
        subtype = event.get("subtype")
        is_error = event.get("is_error")
        session = event.get("session_id") or event.get("sessionId")
        bits = [f"result subtype={subtype}", f"is_error={is_error}"]
        if session:
            bits.append(f"session={session}")
        return " ".join(bits)

    if etype == "user":
        return None

    # Unknown event types: mention type only, never dump payload.
    return f"event type={etype}"


def emit_progress(message: str, stream: TextIO | None = None) -> None:
    stream = stream or sys.stderr
    line = strip_control_chars(message.rstrip("\n"))
    if not line:
        return
    try:
        print(f"[handoff] {line}", file=stream, flush=True)
    except UnicodeEncodeError:
        encoded = f"[handoff] {line}\n".encode(stream.encoding or "utf-8", errors="replace")
        try:
            stream.buffer.write(encoded)
            stream.buffer.flush()
        except Exception:
            pass


class ProgressReporter:
    """Human-facing progress sink used by --live and watch.py."""

    def __init__(self, stream: TextIO | None = None, *, prefix_enabled: bool = True) -> None:
        self.stream = stream or sys.stderr
        self.prefix_enabled = prefix_enabled
        self._seen_event_keys: set[str] = set()

    def emit_line(self, message: str) -> None:
        if self.prefix_enabled:
            emit_progress(message, self.stream)
        else:
            line = strip_control_chars(message.rstrip("\n"))
            if line:
                print(line, file=self.stream, flush=True)

    def announce_start(
        self,
        *,
        workspace: str,
        task_snapshot: str,
        trust_workspace: bool,
        run_dir: str | None = None,
    ) -> None:
        self.emit_line(f"workspace: {workspace}")
        self.emit_line(f"task: {task_snapshot}")
        self.emit_line(f"trust_workspace: {bool(trust_workspace)}")
        if run_dir:
            self.emit_line(f"run: {run_dir}")

    def announce_pid(self, pid: int) -> None:
        self.emit_line(f"pid: {pid}")

    def announce_viewer(self, info: dict) -> None:
        if info.get("launched"):
            self.emit_line(f"viewer launched pid={info.get('pid')}")
        elif info.get("manual_command"):
            self.emit_line(f"viewer manual: {info['manual_command']}")
        if info.get("error"):
            self.emit_line(f"viewer error: {info['error']}")
        if info.get("note"):
            self.emit_line(str(info["note"]))

    def on_event(self, event: dict) -> None:
        summary = summarize_event(event)
        if not summary:
            return
        session = event.get("session_id") or event.get("sessionId")
        if session and "session=" not in summary:
            summary = f"{summary} session={session}"
        # Dedupe only by call/event id + subtype (file offsets already skip rereads).
        # Identical tool summaries with distinct call ids must both appear.
        dedupe_key = _event_dedupe_key(event)
        if dedupe_key is not None:
            if dedupe_key in self._seen_event_keys:
                return
            self._seen_event_keys.add(dedupe_key)
        self.emit_line(summary)

    def on_stderr_text(self, text: str) -> None:
        for raw in text.splitlines():
            line = strip_control_chars(raw).strip()
            if not line:
                continue
            self.emit_line(f"stderr: {line}")

    def on_status(self, status: dict) -> None:
        state = status.get("state")
        if not state:
            return
        bits = [f"state: {state}"]
        if status.get("block_reason"):
            bits.append(f"block_reason={status['block_reason']}")
        if status.get("session_id"):
            bits.append(f"session={status['session_id']}")
        if status.get("exit_code") is not None:
            bits.append(f"exit_code={status['exit_code']}")
        if status.get("error"):
            bits.append(f"error={status['error']}")
        if status.get("next_action") and state == "blocked":
            bits.append(f"next_action={status['next_action']}")
        self.emit_line(" | ".join(bits))

    def show_task_snapshot(self, task_path: Path) -> None:
        try:
            text = task_path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            self.emit_line(f"task snapshot unreadable: {exc}")
            return
        self.emit_line("--- task request (snapshot) ---")
        for line in text.splitlines() or [""]:
            self.emit_line(strip_control_chars(line))
        self.emit_line("--- end task snapshot ---")

def read_json_object(path: Path) -> dict | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def iter_new_jsonl_events(path: Path, offset: int) -> tuple[list[dict], int]:
    """Read new complete JSONL lines from byte offset; skip partial trailing line.

    Offsets advance by raw byte length before any UTF-8 decode so invalid or
    split multi-byte sequences do not desync the next read.
    """
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read()
    except OSError:
        return [], offset
    if not data:
        return [], offset
    if not data.endswith(b"\n"):
        last_nl = data.rfind(b"\n")
        if last_nl < 0:
            return [], offset
        data = data[: last_nl + 1]
    # Byte-accurate: compute before decoding (invalid UTF-8 must not shift offset).
    new_offset = offset + len(data)
    events: list[dict] = []
    for raw_line in data.split(b"\n"):
        if not raw_line.strip():
            continue
        try:
            line = raw_line.decode("utf-8", errors="replace").strip()
        except UnicodeError:
            continue
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events, new_offset


def read_new_text(path: Path, offset: int) -> tuple[str, int]:
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read()
            new_offset = handle.tell()
    except OSError:
        return "", offset
    if not data:
        return "", offset
    return data.decode("utf-8", errors="replace"), new_offset
