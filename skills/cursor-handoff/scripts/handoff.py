#!/usr/bin/env python3
"""Portable Cursor handoff runner: execute a scoped task via Cursor CLI.

Supported Cursor stream-json result schema (success):
  {"type": "result", "subtype": "success", "is_error": false, ...}

A result counts as success only when type is "result", subtype is exactly
"success", and is_error is explicitly false. Any other result shape is treated
as failure. A prior error/malformed result is never replaced by a later success.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

PROTECTED_STATES = frozenset({"timeout", "interrupted"})
SUCCESS_STATE = "execution_completed"
LOCK_NAME = "workspace.lock"
REQUIRED_FLAGS = ("--print", "--output-format", "--workspace")
OPTIONAL_FLAGS = ("--auto-review", "--trust", "--model")
CAPABILITY_FLAGS = REQUIRED_FLAGS + OPTIONAL_FLAGS
WINDOWS_TASKKILL_TIMEOUT = 30.0


class RunnerError(Exception):
    """User-facing runner failure."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def configure_stdio_utf8() -> None:
    """Prefer UTF-8 on stdout/stderr when the host allows reconfigure (Windows cp949 etc.)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError, AttributeError):
                pass


def emit_json(data: dict, *, indent: int | None = None) -> None:
    """Print JSON safely on legacy Windows code pages via ASCII escapes."""
    text = json.dumps(data, indent=indent, ensure_ascii=True)
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(text.encode("ascii", errors="replace") + b"\n")
        sys.stdout.buffer.flush()


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, data: dict) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def is_unsafe_shell_script(path: Path) -> bool:
    return path.suffix.lower() in {".cmd", ".bat"}


def absolute_path(path: Path) -> Path:
    expanded = path.expanduser()
    try:
        return expanded.resolve()
    except OSError:
        return expanded.absolute()


def candidate_agent_paths() -> list[Path]:
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

    for name in ("cursor-agent", "agent"):
        which = shutil.which(name)
        if which:
            add(Path(which))

    home = Path.home()
    for name in ("cursor-agent", "agent"):
        add(home / ".local" / "bin" / name)

    localappdata = os.environ.get("LOCALAPPDATA")
    if localappdata:
        add(Path(localappdata) / "cursor-agent" / "agent.ps1")

    return found


def accept_agent_path(candidate: Path, source: str) -> Path:
    if is_unsafe_shell_script(candidate):
        raise RunnerError(
            f"Rejected unsafe shell launcher from {source}: {candidate}. "
            "Use a real executable or Windows agent.ps1 and invoke via pwsh/powershell. "
            "Safe cmd/bat dispatch is not implemented."
        )
    if not candidate.is_file():
        raise RunnerError(f"Agent path not found ({source}): {candidate}")
    return absolute_path(candidate)


def discover_agent(explicit: Path | None) -> Path:
    """Resolve the agent. Explicit --agent-path and CURSOR_AGENT_PATH are authoritative."""
    if explicit is not None:
        return accept_agent_path(explicit.expanduser(), "--agent-path")

    env = os.environ.get("CURSOR_AGENT_PATH")
    if env:
        return accept_agent_path(Path(env).expanduser(), "CURSOR_AGENT_PATH")

    errors: list[str] = []
    for candidate in candidate_agent_paths():
        if is_unsafe_shell_script(candidate):
            errors.append(
                f"Rejected unsafe shell launcher {candidate}. "
                "Use a real executable or Windows agent.ps1 and invoke via pwsh/powershell."
            )
            continue
        if candidate.is_file():
            return absolute_path(candidate)
    detail = " ".join(errors) if errors else "No cursor-agent/agent executable found."
    raise RunnerError(
        detail
        + " Checked PATH, ~/.local/bin, and %LOCALAPPDATA%/cursor-agent/agent.ps1 when present. "
        "Pass --agent-path or set CURSOR_AGENT_PATH."
    )


def powershell_host() -> str:
    host = shutil.which("pwsh") or shutil.which("powershell")
    if not host:
        raise RunnerError("PowerShell (pwsh or powershell) is required to run agent.ps1.")
    return str(absolute_path(Path(host)))


def agent_prefix(agent: Path) -> list[str]:
    if agent.suffix.lower() == ".ps1":
        return [powershell_host(), "-NoProfile", "-File", str(agent)]
    if is_unsafe_shell_script(agent):
        raise RunnerError(
            f"Refusing to dispatch {agent.name}. Safe cmd/bat execution is not implemented; "
            "point --agent-path at agent.ps1 or a native executable."
        )
    return [str(agent)]


def run_bounded(command: list[str], timeout: float = 20.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        shell=False,
    )


def probe_capabilities(agent: Path) -> dict:
    prefix = agent_prefix(agent)
    info = {
        "agent": str(agent),
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
            info["version"] = (version.stdout or version.stderr or "").strip().splitlines()[:1]
            info["version"] = info["version"][0] if info["version"] else ""
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
            for flag in CAPABILITY_FLAGS:
                info["supported_flags"][flag] = flag in help_text
        else:
            info["notes"].append("help probe returned nonzero or empty output")
    except (OSError, subprocess.TimeoutExpired) as exc:
        info["notes"].append(f"help probe failed: {exc}")
    return info


def require_capabilities(capabilities: dict) -> None:
    if not capabilities.get("help_ok"):
        notes = "; ".join(capabilities.get("notes") or []) or "no details"
        raise RunnerError(
            "Cursor CLI help probe failed or timed out; refusing to dispatch unverified flags. "
            f"Details: {notes}"
        )
    flags = capabilities.get("supported_flags") or {}
    missing = [flag for flag in REQUIRED_FLAGS if not flags.get(flag)]
    if missing:
        raise RunnerError(
            "Installed Cursor CLI is missing required flags: "
            + ", ".join(missing)
            + ". Upgrade cursor-agent or point --agent-path at a compatible CLI."
        )


def path_escapes(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return False
    except (ValueError, OSError):
        return True


def assert_within_workspace(path: Path, workspace: Path, label: str) -> None:
    if path_escapes(path, workspace):
        raise RunnerError(
            f"{label} resolves outside the workspace "
            f"(symlink/junction-aware). workspace={workspace.resolve()} path={path}"
        )


def validate_handoff_dir(workspace: Path) -> Path:
    """Reject .cursor-handoff that escapes the workspace before creating logs/locks."""
    handoff_dir = workspace / ".cursor-handoff"
    if handoff_dir.exists() or handoff_dir.is_symlink():
        assert_within_workspace(handoff_dir, workspace, ".cursor-handoff")
        current = handoff_dir
        # Walk parents within workspace for symlink/junction ancestry.
        for _ in range(len(handoff_dir.parts)):
            if current == workspace or current == current.parent:
                break
            if current.is_symlink():
                assert_within_workspace(current, workspace, f"symlink {current}")
            current = current.parent
    return handoff_dir


def validate_paths(workspace: Path, task: Path) -> tuple[Path, Path, str]:
    if not workspace.exists():
        raise RunnerError(f"Workspace does not exist: {workspace}")
    if not workspace.is_dir():
        raise RunnerError(f"Workspace must be a directory: {workspace}")
    workspace_resolved = workspace.resolve()
    if not task.exists() or not task.is_file():
        raise RunnerError(f"Task must be an existing file: {task}")
    task_resolved = task.resolve()
    try:
        task_resolved.relative_to(workspace_resolved)
    except ValueError as exc:
        raise RunnerError(
            "Task must resolve inside the workspace (symlink-aware). "
            f"workspace={workspace_resolved} task={task_resolved}"
        ) from exc
    content = task_resolved.read_text(encoding="utf-8-sig")
    if not content.strip():
        raise RunnerError("Task must not be empty.")
    return workspace_resolved, task_resolved, content


def lock_path_for(workspace: Path) -> Path:
    return workspace / ".cursor-handoff" / LOCK_NAME


def acquire_lock(workspace: Path, run_id: str) -> Path:
    handoff_dir = validate_handoff_dir(workspace)
    handoff_dir.mkdir(parents=True, exist_ok=True)
    assert_within_workspace(handoff_dir, workspace, ".cursor-handoff")
    lock_path = lock_path_for(workspace)
    payload = {
        "pid": os.getpid(),
        "run_id": run_id,
        "created_at": utc_now(),
        "workspace": str(workspace),
    }
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(str(lock_path), flags)
    except FileExistsError as exc:
        existing = ""
        try:
            existing = lock_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            existing = "(unreadable)"
        raise RunnerError(
            "Workspace handoff lock already exists at "
            f"{lock_path}. Another runner may be active or a previous run left a stale lock. "
            "Confirm no handoff process is running for this workspace, then remove that lock "
            "file manually if it is stale. This runner will not delete another runner's lock. "
            f"Existing lock contents: {existing}"
        ) from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, indent=2) + "\n")
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    return lock_path


def release_lock(lock_path: Path | None, expected_pid: int) -> None:
    if lock_path is None or not lock_path.exists():
        return
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
        if int(data.get("pid", -1)) != expected_pid:
            return
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return
    try:
        lock_path.unlink()
    except OSError:
        pass


def kill_process_tree(proc: subprocess.Popen[bytes]) -> None:
    """Stop the spawned process and descendants. Parent exit alone is not enough on POSIX."""
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                shell=False,
                timeout=WINDOWS_TASKKILL_TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pass
        return

    # POSIX: always signal the process group so descendants stop even if the
    # parent handles TERM and exits quickly.
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        pass


def build_prompt(task_rel: str) -> str:
    return (
        f"Read the UTF-8 task file at {task_rel}. "
        "Complete only that task within this workspace. "
        "Preserve unrelated changes. "
        "Do not commit, push, publish, use external messaging, or access credentials. "
        "Report changed files, actual validation commands and results, and any blockers."
    )


def build_agent_command(
    agent: Path,
    workspace: Path,
    prompt: str,
    model: str | None,
    trust_workspace: bool,
    capabilities: dict,
) -> list[str]:
    require_capabilities(capabilities)
    command = agent_prefix(agent)
    flags = capabilities.get("supported_flags") or {}
    required_missing: list[str] = []

    def require(flag: str, *extra: str) -> None:
        if not flags.get(flag):
            required_missing.append(flag)
            return
        command.append(flag)
        command.extend(extra)

    require("--workspace", str(workspace))
    require("--print")
    require("--output-format", "stream-json")
    if flags.get("--auto-review"):
        command.append("--auto-review")
    if trust_workspace:
        require("--trust")
    if model:
        if not flags.get("--model"):
            required_missing.append("--model")
        else:
            command.extend(["--model", model])
    command.append(prompt)

    if required_missing:
        raise RunnerError(
            "Installed Cursor CLI appears to lack required flags: "
            + ", ".join(required_missing)
            + ". Upgrade cursor-agent or adjust options. Help probe did not advertise these flags."
        )
    return command


def save_status(status_file: Path, status: dict) -> None:
    atomic_write_json(status_file, status)


def result_is_success(event: dict) -> bool:
    return (
        event.get("type") == "result"
        and event.get("subtype") == "success"
        and event.get("is_error") is False
    )


def consume_stdout(
    stream,
    events_path: Path,
    result_path: Path,
    status: dict,
    status_file: Path,
    status_lock: threading.Lock,
    reader_errors: list,
) -> None:
    try:
        with events_path.open("wb") as out:
            while True:
                line = stream.readline()
                if not line:
                    break
                out.write(line)
                out.flush()
                try:
                    text = line.decode("utf-8", errors="replace").strip()
                    if not text:
                        continue
                    event = json.loads(text)
                except (UnicodeError, json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("type") != "result":
                    continue
                atomic_write_json(result_path, event)
                with status_lock:
                    status["result_file"] = str(result_path)
                    session_id = event.get("session_id") or event.get("sessionId")
                    if session_id:
                        status["session_id"] = session_id
                    if status.get("state") in PROTECTED_STATES:
                        save_status(status_file, status)
                        continue
                    if result_is_success(event):
                        if status.get("agent_is_error"):
                            # Prior error/malformed result must not be erased.
                            status["state"] = "failed"
                        else:
                            status["agent_is_error"] = False
                    else:
                        status["agent_is_error"] = True
                        status["state"] = "failed"
                        if event.get("is_error") is not True or event.get("subtype") == "success":
                            status["error"] = (
                                "Malformed or unsuccessful result event; "
                                "require type=result, subtype=success, is_error=false"
                            )
                    save_status(status_file, status)
    except Exception as exc:
        reader_errors.append(exc)
        with status_lock:
            if status.get("state") not in PROTECTED_STATES:
                status["state"] = "runner_error"
            status["error"] = f"Reader failure: {exc}"
            try:
                save_status(status_file, status)
            except Exception as save_exc:
                reader_errors.append(save_exc)


def finalize_success_state(status: dict) -> None:
    if status.get("state") in PROTECTED_STATES:
        return
    if status.get("state") == "runner_error":
        return
    exit_code = status.get("exit_code")
    if exit_code not in (0, None) and exit_code != 0:
        status["state"] = "failed"
        return
    if exit_code == 0:
        if not status.get("result_file"):
            status["state"] = "failed"
            status["error"] = "Missing result event; CLI exit 0 is not success without result.json"
            return
        if status.get("agent_is_error"):
            status["state"] = "failed"
            return
        # Re-check stored result file against the supported success schema.
        try:
            event = json.loads(Path(status["result_file"]).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            status["state"] = "failed"
            status["error"] = "Unreadable or invalid result.json"
            status["agent_is_error"] = True
            return
        if not isinstance(event, dict) or not result_is_success(event):
            status["state"] = "failed"
            status["agent_is_error"] = True
            status["error"] = (
                "Result event is not a supported success "
                "(need type=result, subtype=success, is_error=false)"
            )
            return
        status["state"] = SUCCESS_STATE
        status["note"] = (
            "execution_completed means the Cursor CLI finished with a non-error result event; "
            "it is not review acceptance of the task outcome."
        )


def run_handoff(args: argparse.Namespace) -> int:
    workspace, task, content = validate_paths(args.workspace, args.task)
    if args.timeout < 1:
        raise RunnerError("Timeout must be a positive integer (seconds).")

    # Resolve agent before any cwd change for the child process.
    agent = discover_agent(args.agent_path)
    capabilities = probe_capabilities(agent)
    require_capabilities(capabilities)

    if args.dry_run:
        task_rel = task.relative_to(workspace).as_posix()
        prompt = build_prompt(task_rel)
        command = build_agent_command(
            agent=agent,
            workspace=workspace,
            prompt=prompt,
            model=args.model,
            trust_workspace=args.trust_workspace,
            capabilities=capabilities,
        )
        payload = {
            "mode": "dry-run",
            "workspace": str(workspace),
            "task": str(task),
            "agent": str(agent),
            "timeout": args.timeout,
            "trust_workspace": bool(args.trust_workspace),
            "model": args.model,
            "command": command[:-1] + ["<prompt>"],
            "notes": [
                "Dry-run does not create run folders, locks, or mutate the workspace.",
                "Task contents are intentionally omitted.",
                "Workspace path is not an OS sandbox; host approval and Cursor permissions still apply.",
            ],
            "capabilities": {
                "version": capabilities.get("version"),
                "supported_flags": capabilities.get("supported_flags"),
                "notes": capabilities.get("notes"),
            },
        }
        emit_json(payload, indent=2)
        return 0

    run_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    run_dir = workspace / ".cursor-handoff" / run_id
    lock_path: Path | None = None
    status: dict = {
        "state": "running",
        "workspace": str(workspace),
        "run": str(run_dir),
        "started_at": utc_now(),
        "trust_workspace": bool(args.trust_workspace),
        "agent": str(agent),
    }
    status_file = run_dir / "status.json"
    status_lock = threading.Lock()
    proc: subprocess.Popen[bytes] | None = None
    stderr_handle = None
    reader: threading.Thread | None = None
    reader_errors: list = []
    exit_status = 1

    try:
        lock_path = acquire_lock(workspace, run_id)
        run_dir.mkdir(parents=True, exist_ok=False)
        assert_within_workspace(run_dir, workspace, "run directory")
        frozen_task = run_dir / "task.md"
        frozen_task.write_text(content, encoding="utf-8", newline="\n")
        # Build the execution command only after the snapshot exists so Cursor
        # reads the frozen run/task.md, not a live original that may change.
        frozen_rel = frozen_task.relative_to(workspace).as_posix()
        prompt = build_prompt(frozen_rel)
        command = build_agent_command(
            agent=agent,
            workspace=workspace,
            prompt=prompt,
            model=args.model,
            trust_workspace=args.trust_workspace,
            capabilities=capabilities,
        )
        status["task_snapshot"] = frozen_rel
        save_status(status_file, status)
        emit_json({"run": str(run_dir), "state": "running"})

        popen_kwargs: dict = {
            "cwd": str(workspace),
            "stdout": subprocess.PIPE,
            "stdin": subprocess.DEVNULL,
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
        save_status(status_file, status)

        assert proc.stdout is not None
        reader = threading.Thread(
            target=consume_stdout,
            args=(
                proc.stdout,
                run_dir / "events.jsonl",
                run_dir / "result.json",
                status,
                status_file,
                status_lock,
                reader_errors,
            ),
            daemon=True,
        )
        try:
            reader.start()
        except Exception:
            kill_process_tree(proc)
            raise

        try:
            try:
                exit_code = proc.wait(timeout=args.timeout)
                with status_lock:
                    status["exit_code"] = exit_code
                    if status.get("state") not in PROTECTED_STATES:
                        if exit_code != 0:
                            status["state"] = "failed"
                    save_status(status_file, status)
            except subprocess.TimeoutExpired:
                with status_lock:
                    status["state"] = "timeout"
                    save_status(status_file, status)
                kill_process_tree(proc)
                with status_lock:
                    status["exit_code"] = proc.poll()
                    status["finished_at"] = utc_now()
                    save_status(status_file, status)
            except KeyboardInterrupt:
                with status_lock:
                    status["state"] = "interrupted"
                    status["finished_at"] = utc_now()
                    save_status(status_file, status)
                kill_process_tree(proc)
                with status_lock:
                    status["exit_code"] = proc.poll()
                    if "finished_at" not in status:
                        status["finished_at"] = utc_now()
                    save_status(status_file, status)
                exit_status = 130
                emit_json(status)
                return exit_status
        finally:
            if reader is not None:
                reader.join(timeout=30)
            if proc is not None:
                if proc.stdout is not None:
                    try:
                        proc.stdout.close()
                    except Exception:
                        pass
                    proc.stdout = None
                if proc.poll() is None:
                    kill_process_tree(proc)
            if stderr_handle is not None:
                try:
                    stderr_handle.close()
                except Exception:
                    pass
                stderr_handle = None

        with status_lock:
            if reader is not None and reader.is_alive():
                if status.get("state") not in PROTECTED_STATES:
                    status["state"] = "runner_error"
                status["error"] = "Reader thread did not finish before status finalization"
            elif reader_errors:
                if status.get("state") not in PROTECTED_STATES:
                    status["state"] = "runner_error"
                if not status.get("error"):
                    status["error"] = f"Reader failure: {reader_errors[0]}"
            finalize_success_state(status)
            if "finished_at" not in status:
                status["finished_at"] = utc_now()
            save_status(status_file, status)

        emit_json(status)
        return 0 if status.get("state") == SUCCESS_STATE else 1

    except RunnerError as exc:
        if proc is not None:
            kill_process_tree(proc)
        with status_lock:
            if status.get("state") not in PROTECTED_STATES:
                status["state"] = "runner_error"
            status["error"] = str(exc)
            status["finished_at"] = utc_now()
        if status_file.parent.exists():
            try:
                save_status(status_file, status)
            except Exception:
                pass
        emit_json(status)
        return 1
    except Exception as exc:
        if proc is not None:
            kill_process_tree(proc)
        with status_lock:
            if status.get("state") not in PROTECTED_STATES:
                status["state"] = "runner_error"
            status["error"] = str(exc)
            status["finished_at"] = utc_now()
        if status_file.parent.exists():
            try:
                save_status(status_file, status)
            except Exception:
                pass
        emit_json(status)
        return 1
    finally:
        if proc is not None and getattr(proc, "stdout", None) is not None:
            try:
                proc.stdout.close()
            except Exception:
                pass
            try:
                proc.stdout = None
            except Exception:
                pass
        if stderr_handle is not None:
            try:
                stderr_handle.close()
            except Exception:
                pass
        # Confirm reader completion before releasing the workspace lock.
        if reader is not None and reader.is_alive():
            reader.join(timeout=5)
        release_lock(lock_path, os.getpid())


def doctor(args: argparse.Namespace) -> int:
    report: dict = {
        "mode": "doctor",
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "agent_found": False,
        "authentication": "not checked (doctor does not login or write config)",
        "notes": [
            "Doctor reports installation/capability status only; it is not an authentication guarantee.",
            "Workspace path is not an OS sandbox; host Cursor approvals still apply at runtime.",
            "Supported success result schema: type=result, subtype=success, is_error=false.",
        ],
        "ok": False,
    }
    try:
        agent = discover_agent(args.agent_path)
        report["agent_found"] = True
        report["agent"] = str(agent)
        capabilities = probe_capabilities(agent)
        report["version"] = capabilities.get("version")
        report["supported_flags"] = capabilities.get("supported_flags")
        report["capability_notes"] = capabilities.get("notes")
        report["help_ok"] = bool(capabilities.get("help_ok"))
        require_capabilities(capabilities)
        if capabilities.get("supported_flags") and not capabilities["supported_flags"].get("--auto-review"):
            report["notes"].append("--auto-review not advertised; runner will omit it.")
        report["ok"] = True
    except RunnerError as exc:
        report["error"] = str(exc)
        emit_json(report, indent=2)
        return 1
    emit_json(report, indent=2)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a scoped Cursor task and retain inspectable output."
    )
    parser.add_argument("--workspace", type=Path, help="Target workspace directory")
    parser.add_argument("--task", type=Path, help="UTF-8 task file inside the workspace")
    parser.add_argument("--timeout", type=int, default=600, help="Seconds before process-tree kill")
    parser.add_argument("--model", help="Optional Cursor model id")
    parser.add_argument(
        "--agent-path",
        type=Path,
        help="Cursor agent executable or agent.ps1 (authoritative; overrides discovery)",
    )
    parser.add_argument(
        "--trust-workspace",
        action="store_true",
        help="Pass Cursor --trust for this workspace only (default: off)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate inputs and print dispatch plan without creating runs or locks",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Report agent installation/capability status without login or config writes",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_stdio_utf8()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.doctor:
            return doctor(args)
        if args.workspace is None or args.task is None:
            parser.error("--workspace and --task are required unless --doctor is set")
        return run_handoff(args)
    except RunnerError as exc:
        emit_json({"state": "runner_error", "error": str(exc)})
        return 1
    except KeyboardInterrupt:
        emit_json({"state": "interrupted", "error": "Interrupted", "finished_at": utc_now()})
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
