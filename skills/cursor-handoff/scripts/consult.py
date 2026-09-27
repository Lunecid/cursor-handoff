#!/usr/bin/env python3
"""Optional Claude Code consult runner: independent design critique via Claude CLI.

Provider success is recorded as advice_received only. That is not consensus, agreement,
or authorization to change scope. Advice is untrusted input for the coordinator.

Success schema (same family as Cursor result events):
  type=result, subtype=success, is_error=false, and process exit code 0.

subtype=success alone is NOT enough: is_error and exit code take precedence
(e.g. quota/login blockers with is_error=true must not become advice_received).
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid

SCRIPT_DIR = Path(__file__).resolve().parent
_HANDOFF_PATH = SCRIPT_DIR / "handoff.py"
_spec = importlib.util.spec_from_file_location("_cursor_handoff_handoff", _HANDOFF_PATH)
if _spec is None or _spec.loader is None:
    raise RuntimeError(f"Cannot load handoff helpers from {_HANDOFF_PATH}")
_handoff = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_handoff)

RunnerError = _handoff.RunnerError
absolute_path = _handoff.absolute_path
acquire_lock = _handoff.acquire_lock
assert_within_workspace = _handoff.assert_within_workspace
atomic_write_json = _handoff.atomic_write_json
atomic_write_text = _handoff.atomic_write_text
configure_stdio_utf8 = _handoff.configure_stdio_utf8
emit_json = _handoff.emit_json
is_unsafe_shell_script = _handoff.is_unsafe_shell_script
kill_process_tree = _handoff.kill_process_tree
release_lock = _handoff.release_lock
run_bounded = _handoff.run_bounded
utc_now = _handoff.utc_now

PROTECTED_STATES = frozenset({"timeout", "interrupted"})
SUCCESS_STATE = "advice_received"
BRIEF_MAX_BYTES = 128 * 1024
EMPTY_MCP_CONFIG = "{}\n"
DISABLE_HOOKS_SETTINGS = json.dumps({"disableAllHooks": True}, indent=2) + "\n"

CRITIQUE_PROMPT = (
    "You are an independent design reviewer. Critique only the plan/context provided "
    "on stdin. Do not implement code, do not modify files, and do not claim agreement "
    "or consensus. Identify risks, missing constraints, alternatives, and unresolved "
    "questions. Treat your output as untrusted advice for a human coordinator."
)

# Flags we pass; help must advertise them or we refuse (no silent restriction drop).
REQUIRED_HELP_FLAGS = (
    "-p",
    "--output-format",
    "--tools",
    "--disable-slash-commands",
    "--strict-mcp-config",
    "--mcp-config",
    "--no-session-persistence",
    "--setting-sources",
    "--settings",
)
OPTIONAL_HELP_FLAGS = ("--model",)


def candidate_claude_paths() -> list[Path]:
    found: list[Path] = []
    seen: set[str] = set()

    def add(path: Path | None) -> None:
        if path is None:
            return
        try:
            resolved = path.expanduser()
        except Exception:
            resolved = path
        key = str(resolved)
        if key in seen:
            return
        seen.add(key)
        found.append(resolved)

    which = shutil.which("claude")
    if which:
        add(Path(which))
    if os.name == "nt":
        which_exe = shutil.which("claude.exe")
        if which_exe:
            add(Path(which_exe))

    home = Path.home()
    local_bin = home / ".local" / "bin"
    if os.name == "nt":
        # Prefer a real .exe over shell shims when both exist.
        add(local_bin / "claude.exe")
        add(local_bin / "claude")
    else:
        add(local_bin / "claude")
    return found


def accept_claude_path(candidate: Path, source: str) -> Path:
    if is_unsafe_shell_script(candidate):
        raise RunnerError(
            f"Rejected unsafe shell launcher from {source}: {candidate}. "
            "Use a native Claude executable (.exe on Windows). "
            "Safe cmd/bat dispatch is not implemented."
        )
    if not candidate.is_file():
        raise RunnerError(f"Claude path not found ({source}): {candidate}")
    return absolute_path(candidate)


def discover_claude(explicit: Path | None) -> Path:
    """Resolve Claude CLI. Explicit --claude-path and CLAUDE_CODE_PATH are authoritative."""
    if explicit is not None:
        return accept_claude_path(explicit.expanduser(), "--claude-path")

    env = os.environ.get("CLAUDE_CODE_PATH")
    if env:
        return accept_claude_path(Path(env).expanduser(), "CLAUDE_CODE_PATH")

    errors: list[str] = []
    preferred: Path | None = None
    fallback: Path | None = None
    for candidate in candidate_claude_paths():
        if is_unsafe_shell_script(candidate):
            errors.append(
                f"Rejected unsafe shell launcher {candidate}. "
                "Use a native Claude executable."
            )
            continue
        if not candidate.is_file():
            continue
        resolved = absolute_path(candidate)
        if os.name == "nt" and resolved.suffix.lower() == ".exe":
            preferred = resolved
            break
        if fallback is None:
            fallback = resolved
    if preferred is not None:
        return preferred
    if fallback is not None:
        # POSIX executable or non-.exe Windows binary that is not cmd/bat.
        return fallback
    detail = " ".join(errors) if errors else "No claude executable found."
    raise RunnerError(
        detail
        + " Checked PATH and ~/.local/bin/claude[.exe]. "
        "Pass --claude-path or set CLAUDE_CODE_PATH."
    )


def claude_prefix(claude: Path) -> list[str]:
    if is_unsafe_shell_script(claude):
        raise RunnerError(
            f"Refusing to dispatch {claude.name}. Safe cmd/bat execution is not implemented."
        )
    # Allow a .py wrapper (tests / thin launchers) via the current interpreter.
    if claude.suffix.lower() == ".py":
        return [sys.executable, str(claude)]
    return [str(claude)]


def probe_capabilities(claude: Path) -> dict:
    prefix = claude_prefix(claude)
    info = {
        "claude": str(claude),
        "prefix": prefix,
        "version": None,
        "help_ok": False,
        "supported_flags": {},
        "help_text": "",
        "notes": [],
    }
    try:
        version = run_bounded(prefix + ["--version"], timeout=15.0)
        if version.returncode == 0:
            line = (version.stdout or version.stderr or "").strip().splitlines()[:1]
            info["version"] = line[0] if line else ""
        else:
            info["notes"].append("version probe returned nonzero")
    except (OSError, subprocess.TimeoutExpired) as exc:
        info["notes"].append(f"version probe failed: {exc}")

    try:
        help_proc = run_bounded(prefix + ["--help"], timeout=15.0)
        help_text = (help_proc.stdout or "") + (help_proc.stderr or "")
        info["help_text"] = help_text
        info["help_ok"] = help_proc.returncode == 0 and bool(help_text.strip())
        if info["help_ok"]:
            for flag in REQUIRED_HELP_FLAGS + OPTIONAL_HELP_FLAGS:
                info["supported_flags"][flag] = flag in help_text
            # Accept --print as satisfying -p when short form is absent from help prose.
            if not info["supported_flags"].get("-p") and "--print" in help_text:
                info["supported_flags"]["-p"] = True
                info["notes"].append("help lists --print; treating as -p support")
        else:
            info["notes"].append("help probe returned nonzero or empty output")
    except (OSError, subprocess.TimeoutExpired) as exc:
        info["notes"].append(f"help probe failed: {exc}")
    return info


def require_capabilities(capabilities: dict) -> None:
    if not capabilities.get("help_ok"):
        notes = "; ".join(capabilities.get("notes") or []) or "no details"
        raise RunnerError(
            "Claude CLI help probe failed or timed out; refusing to dispatch unverified flags. "
            f"Details: {notes}"
        )
    flags = capabilities.get("supported_flags") or {}
    missing = [flag for flag in REQUIRED_HELP_FLAGS if not flags.get(flag)]
    if missing:
        raise RunnerError(
            "Installed Claude CLI is missing required flags: "
            + ", ".join(missing)
            + ". Upgrade Claude Code or point --claude-path at a compatible CLI. "
            "Refusing to drop safety restrictions."
        )


def validate_brief(workspace: Path, brief: Path) -> tuple[Path, Path, str, bytes]:
    if not workspace.exists():
        raise RunnerError(f"Workspace does not exist: {workspace}")
    if not workspace.is_dir():
        raise RunnerError(f"Workspace must be a directory: {workspace}")
    workspace_resolved = workspace.resolve()
    if not brief.exists() or not brief.is_file():
        raise RunnerError(f"Brief must be an existing file: {brief}")
    brief_resolved = brief.resolve()
    try:
        brief_resolved.relative_to(workspace_resolved)
    except ValueError as exc:
        raise RunnerError(
            "Brief must resolve inside the workspace (symlink-aware). "
            f"workspace={workspace_resolved} brief={brief_resolved}"
        ) from exc
    raw = brief_resolved.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    if len(raw) > BRIEF_MAX_BYTES:
        raise RunnerError(
            f"Brief exceeds size limit of {BRIEF_MAX_BYTES} bytes "
            f"(got {len(raw)} bytes)."
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RunnerError(f"Brief must be UTF-8 text: {exc}") from exc
    if not text.strip():
        raise RunnerError("Brief must not be empty.")
    # Pipe exact file bytes (BOM stripped) so unicode/shell-like characters are unchanged.
    return workspace_resolved, brief_resolved, text, raw


def advice_is_success(event: dict, exit_code: int | None) -> bool:
    if exit_code != 0:
        return False
    return (
        event.get("type") == "result"
        and event.get("subtype") == "success"
        and event.get("is_error") is False
    )


def _path_is_under(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def _candidate_temp_bases(workspace: Path) -> list[Path | None]:
    """Bases for TemporaryDirectory(dir=...). None means the process default temp."""
    workspace_resolved = workspace.resolve()
    default_temp = Path(tempfile.gettempdir()).resolve()
    bases: list[Path | None] = []

    if not _path_is_under(default_temp, workspace_resolved) and default_temp != workspace_resolved:
        bases.append(None)

    alts: list[Path] = []
    if os.name == "nt":
        windir = os.environ.get("WINDIR")
        if windir:
            alts.append(Path(windir) / "Temp")
        local_app = os.environ.get("LOCALAPPDATA")
        if local_app:
            alts.append(Path(local_app) / "Temp")
        alts.append(Path.home() / "AppData" / "Local" / "Temp")
    else:
        alts.extend([Path("/tmp"), Path("/var/tmp"), Path.home() / ".cache"])

    seen: set[str] = set()
    for alt in alts:
        try:
            resolved = alt.resolve()
        except OSError:
            continue
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        if _path_is_under(resolved, workspace_resolved) or resolved == workspace_resolved:
            continue
        if not resolved.exists() or not resolved.is_dir():
            continue
        bases.append(resolved)

    return bases


def create_ephemeral_consult_cwd(workspace: Path) -> tempfile.TemporaryDirectory:
    """Create a runner-owned empty cwd outside the source workspace.

    Caller must clean up via the TemporaryDirectory context manager / cleanup();
    only this owned directory is removed. Home/managed Claude policy may still apply.
    """
    workspace_resolved = workspace.resolve()
    errors: list[str] = []
    for base in _candidate_temp_bases(workspace_resolved):
        kwargs: dict = {"prefix": "cursor-handoff-consult-"}
        if base is not None:
            kwargs["dir"] = str(base)
        try:
            tmp = tempfile.TemporaryDirectory(**kwargs)
        except OSError as exc:
            errors.append(f"base={base or '<default>'}: {exc}")
            continue
        cwd = Path(tmp.name).resolve()
        if _path_is_under(cwd, workspace_resolved) or cwd == workspace_resolved:
            try:
                tmp.cleanup()
            except OSError:
                pass
            errors.append(f"resolved cwd {cwd} is under workspace {workspace_resolved}")
            continue
        return tmp

    detail = "; ".join(errors) if errors else "no usable temp bases"
    raise RunnerError(
        "Cannot create an ephemeral consult working directory outside the workspace. "
        "System temp appears to live inside the workspace (or no alternate temp base worked). "
        f"Details: {detail}. "
        "Refusing to run under the project tree (ancestor CLAUDE.md could load). "
        "This is not an OS sandbox; home/managed Claude policy may still apply."
    )


def extract_advice_text(event: dict) -> str:
    result = event.get("result")
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        for key in ("content", "text", "message"):
            value = result.get(key)
            if isinstance(value, str):
                return value
    return json.dumps(event, indent=2, ensure_ascii=False)


def build_claude_command(
    claude: Path,
    *,
    mcp_config: Path,
    settings_path: Path,
    model: str | None,
    capabilities: dict,
) -> list[str]:
    require_capabilities(capabilities)
    command = claude_prefix(claude)
    flags = capabilities.get("supported_flags") or {}
    # Prefer -p; fall back only if we accepted --print as -p support.
    command.append("-p")
    command.extend(["--output-format", "json"])
    # Empty tool list must be an explicit empty string argument (not omitted).
    command.extend(["--tools", ""])
    command.append("--disable-slash-commands")
    command.append("--strict-mcp-config")
    command.extend(["--mcp-config", str(mcp_config)])
    command.append("--no-session-persistence")
    command.extend(["--setting-sources", ""])
    command.extend(["--settings", str(settings_path)])
    if model:
        if not flags.get("--model"):
            raise RunnerError(
                "Installed Claude CLI does not advertise --model; refusing to pass it."
            )
        command.extend(["--model", model])
    command.append(CRITIQUE_PROMPT)
    return command


def run_consult(args: argparse.Namespace) -> int:
    workspace, brief, content, stdin_bytes = validate_brief(args.workspace, args.brief)
    if args.timeout < 1:
        raise RunnerError("Timeout must be a positive integer (seconds).")

    claude = discover_claude(args.claude_path)
    capabilities = probe_capabilities(claude)
    require_capabilities(capabilities)

    if args.dry_run:
        # Build a dry-run command with placeholder paths; do not create run dirs.
        command = build_claude_command(
            claude,
            mcp_config=Path("<ephemeral-mcp-config>"),
            settings_path=Path("<ephemeral-settings>"),
            model=args.model,
            capabilities=capabilities,
        )
        payload = {
            "mode": "dry-run",
            "workspace": str(workspace),
            "brief": str(brief),
            "claude": str(claude),
            "timeout": args.timeout,
            "model": args.model,
            "command": command[:-1] + ["<critique-prompt>"],
            "stdin": "<brief-omitted>",
            "notes": [
                "Dry-run does not create consult folders, locks, or mutate the workspace.",
                "Brief contents are intentionally omitted.",
                "Consult cwd is a runner-owned TemporaryDirectory outside the source workspace "
                "(not under .cursor-handoff); artifacts and ephemeral configs stay in-workspace.",
                "This is not an OS sandbox; runtime home/managed policy and Claude auth still apply.",
                "Quota or login failures are blockers for the coordinator; do not auto-switch models.",
            ],
            "capabilities": {
                "version": capabilities.get("version"),
                "supported_flags": {
                    k: v
                    for k, v in (capabilities.get("supported_flags") or {}).items()
                    if k in REQUIRED_HELP_FLAGS + OPTIONAL_HELP_FLAGS
                },
                "notes": capabilities.get("notes"),
            },
        }
        emit_json(payload, indent=2)
        return 0

    run_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    run_dir = workspace / ".cursor-handoff" / f"consult-{run_id}"
    lock_path: Path | None = None
    status: dict = {
        "state": "running",
        "workspace": str(workspace),
        "run": str(run_dir),
        "started_at": utc_now(),
        "claude": str(claude),
        "note": (
            "advice_received means the Claude CLI returned a non-error result event; "
            "it is not consensus, agreement, or scope change authorization."
        ),
    }
    status_file = run_dir / "status.json"
    proc: subprocess.Popen[bytes] | None = None
    stderr_handle = None
    exit_status = 1
    ephemeral_cwd: tempfile.TemporaryDirectory | None = None

    try:
        lock_path = acquire_lock(workspace, f"consult-{run_id}")
        run_dir.mkdir(parents=True, exist_ok=False)
        assert_within_workspace(run_dir, workspace, "consult run directory")

        atomic_write_text(run_dir / "brief.md", content)
        mcp_config = run_dir / "mcp.empty.json"
        settings_path = run_dir / "settings.disable-hooks.json"
        atomic_write_text(mcp_config, EMPTY_MCP_CONFIG)
        atomic_write_text(settings_path, DISABLE_HOOKS_SETTINGS)

        # Absolute ephemeral config paths; process cwd is outside the workspace.
        ephemeral_cwd = create_ephemeral_consult_cwd(workspace)
        consult_cwd = Path(ephemeral_cwd.name).resolve()

        command = build_claude_command(
            claude,
            mcp_config=mcp_config.resolve(),
            settings_path=settings_path.resolve(),
            model=args.model,
            capabilities=capabilities,
        )
        # Do not log the full brief or environment.
        status["command"] = command[:-1] + ["<critique-prompt>"]
        status["brief_snapshot"] = "brief.md"
        status["consult_cwd"] = str(consult_cwd)
        status["consult_cwd_note"] = (
            "Runner-owned temp directory outside the workspace; cleaned up after the process. "
            "Not an OS sandbox — home/managed Claude policy may still apply."
        )
        atomic_write_json(status_file, status)
        emit_json({"run": str(run_dir), "state": "running"})

        popen_kwargs: dict = {
            "cwd": str(consult_cwd),
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "shell": False,
        }
        if os.name != "nt":
            popen_kwargs["start_new_session"] = True

        try:
            stderr_handle = (run_dir / "stderr.log").open("wb")
            popen_kwargs["stderr"] = stderr_handle
            proc = subprocess.Popen(command, **popen_kwargs)
        except Exception:
            if stderr_handle is not None:
                try:
                    stderr_handle.close()
                except Exception:
                    pass
                stderr_handle = None
            raise

        status["pid"] = proc.pid
        atomic_write_json(status_file, status)

        timed_out = False
        interrupted = False
        stdout_bytes = b""
        try:
            assert proc.stdin is not None
            try:
                stdout_bytes, _ = proc.communicate(input=stdin_bytes, timeout=args.timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                kill_process_tree(proc)
                try:
                    leftover = proc.communicate(timeout=5)
                    stdout_bytes = leftover[0] or b""
                except Exception:
                    stdout_bytes = b""
            except KeyboardInterrupt:
                interrupted = True
                kill_process_tree(proc)
                try:
                    leftover = proc.communicate(timeout=5)
                    stdout_bytes = leftover[0] or b""
                except Exception:
                    stdout_bytes = b""
        finally:
            if proc is not None:
                if proc.stdout is not None:
                    try:
                        proc.stdout.close()
                    except Exception:
                        pass
                    proc.stdout = None
                if proc.stdin is not None:
                    try:
                        proc.stdin.close()
                    except Exception:
                        pass
                    proc.stdin = None
                if proc.poll() is None:
                    kill_process_tree(proc)
            if stderr_handle is not None:
                try:
                    stderr_handle.close()
                except Exception:
                    pass
                stderr_handle = None

        exit_code = proc.poll() if proc is not None else None
        status["exit_code"] = exit_code

        if timed_out:
            status["state"] = "timeout"
            status["finished_at"] = utc_now()
            atomic_write_json(status_file, status)
            emit_json(status)
            return 1

        if interrupted:
            status["state"] = "interrupted"
            status["finished_at"] = utc_now()
            atomic_write_json(status_file, status)
            emit_json(status)
            return 130

        response_text = (stdout_bytes or b"").decode("utf-8", errors="replace").strip()
        response_path = run_dir / "response.json"
        event: dict | None = None
        parse_error: str | None = None
        if not response_text:
            parse_error = "Missing JSON response on Claude stdout"
        else:
            try:
                parsed = json.loads(response_text)
            except json.JSONDecodeError as exc:
                parse_error = f"Malformed JSON response: {exc}"
                atomic_write_text(response_path, response_text + "\n")
            else:
                if not isinstance(parsed, dict):
                    parse_error = "Response JSON must be an object"
                    atomic_write_json(response_path, {"raw": parsed})
                else:
                    event = parsed
                    atomic_write_json(response_path, event)

        if event is not None:
            status["response_file"] = str(response_path)
            session_id = event.get("session_id") or event.get("sessionId")
            if session_id:
                # Do not treat session ids as account identifiers in doctor; still record for debug.
                status["session_id"] = session_id

        if advice_is_success(event or {}, exit_code):
            assert event is not None
            advice = extract_advice_text(event)
            atomic_write_text(run_dir / "advice.md", advice if advice.endswith("\n") else advice + "\n")
            status["state"] = SUCCESS_STATE
            status["advice_file"] = str(run_dir / "advice.md")
            status["agent_is_error"] = False
        else:
            status["state"] = "failed"
            status["agent_is_error"] = True
            if parse_error:
                status["error"] = parse_error
            elif event is not None and event.get("is_error") is True:
                # Quota/login and similar provider errors: surface to coordinator.
                detail = event.get("result") or event.get("error") or "Claude reported is_error=true"
                api_status = event.get("api_error_status")
                if api_status is not None:
                    status["error"] = f"Claude is_error=true (api_error_status={api_status}): {detail}"
                else:
                    status["error"] = f"Claude is_error=true: {detail}"
                status["blocker"] = "quota_or_provider_error"
            elif exit_code not in (0, None) and exit_code != 0:
                status["error"] = f"Claude exited with code {exit_code}"
            else:
                status["error"] = (
                    "Unsuccessful consult result; require exit 0 and "
                    "type=result, subtype=success, is_error=false"
                )

        status["finished_at"] = utc_now()
        atomic_write_json(status_file, status)
        emit_json(status)
        return 0 if status.get("state") == SUCCESS_STATE else 1

    except RunnerError as exc:
        if proc is not None:
            kill_process_tree(proc)
        status["state"] = "runner_error" if status.get("state") not in PROTECTED_STATES else status["state"]
        status["error"] = str(exc)
        status["finished_at"] = utc_now()
        if status_file.parent.exists():
            try:
                atomic_write_json(status_file, status)
            except Exception:
                pass
        emit_json(status)
        return 1
    except Exception as exc:
        if proc is not None:
            kill_process_tree(proc)
        status["state"] = "runner_error" if status.get("state") not in PROTECTED_STATES else status["state"]
        status["error"] = str(exc)
        status["finished_at"] = utc_now()
        if status_file.parent.exists():
            try:
                atomic_write_json(status_file, status)
            except Exception:
                pass
        emit_json(status)
        return 1
    finally:
        if proc is not None:
            for stream_name in ("stdout", "stdin"):
                stream = getattr(proc, stream_name, None)
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass
                    try:
                        setattr(proc, stream_name, None)
                    except Exception:
                        pass
        if stderr_handle is not None:
            try:
                stderr_handle.close()
            except Exception:
                pass
        if ephemeral_cwd is not None:
            try:
                ephemeral_cwd.cleanup()
            except OSError:
                pass
            ephemeral_cwd = None
        release_lock(lock_path, os.getpid())
    return exit_status


def doctor(args: argparse.Namespace) -> int:
    report: dict = {
        "mode": "doctor",
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "claude_found": False,
        "authentication": "not checked (doctor does not login or write config)",
        "notes": [
            "Doctor reports installation/capability status only; it is not an authentication guarantee.",
            "Consult runs in a runner-owned TemporaryDirectory outside the source workspace "
            "(artifacts stay under .cursor-handoff); this is not an OS sandbox and home/managed "
            "policy may still apply.",
            "Quota/login failures are coordinator blockers; do not auto-switch models or providers.",
            "Supported advice success: exit 0 and type=result, subtype=success, is_error=false.",
        ],
        "ok": False,
    }
    try:
        claude = discover_claude(args.claude_path)
        report["claude_found"] = True
        report["claude"] = str(claude)
        capabilities = probe_capabilities(claude)
        report["version"] = capabilities.get("version")
        report["supported_flags"] = {
            k: v
            for k, v in (capabilities.get("supported_flags") or {}).items()
            if k in REQUIRED_HELP_FLAGS + OPTIONAL_HELP_FLAGS
        }
        report["capability_notes"] = capabilities.get("notes")
        report["help_ok"] = bool(capabilities.get("help_ok"))
        require_capabilities(capabilities)
        report["ok"] = True
    except RunnerError as exc:
        report["error"] = str(exc)
        emit_json(report, indent=2)
        return 1
    emit_json(report, indent=2)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Optional Claude Code design consult; advice is not consensus."
    )
    parser.add_argument("--workspace", type=Path, help="Target workspace directory")
    parser.add_argument("--brief", type=Path, help="UTF-8 design brief inside the workspace")
    parser.add_argument("--timeout", type=int, default=300, help="Seconds before process-tree kill")
    parser.add_argument("--model", help="Optional Claude model id (no silent default change)")
    parser.add_argument(
        "--claude-path",
        type=Path,
        help="Claude executable (authoritative; overrides discovery)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate inputs and print plan without creating consult runs or locks",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Report Claude installation/capability status without login or config writes",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_stdio_utf8()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.doctor:
            return doctor(args)
        if args.workspace is None or args.brief is None:
            parser.error("--workspace and --brief are required unless --doctor is set")
        return run_consult(args)
    except RunnerError as exc:
        emit_json({"state": "runner_error", "error": str(exc)})
        return 1
    except KeyboardInterrupt:
        emit_json({"state": "interrupted", "error": "Interrupted", "finished_at": utc_now()})
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
