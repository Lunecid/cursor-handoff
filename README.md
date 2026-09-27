# cursor-handoff

Portable skill and runner that lets a **planner/reviewer** (Codex or Claude Code) hand an agreed implementation plan to **Cursor CLI** for execution, then review the real diff and test evidence.

Optional **Claude consult** is an independent critique step only when the user asks for Claude discussion/review. Claude is never the implementation executor.

```text
  Codex / Claude Code          Claude CLI (optional)       Cursor CLI           Workspace
  (plan + synthesize) --brief--> (critique only)            (edit + run) --writes--> sources/tests
        |                         advice.md                      |
        |                         (untrusted)                    |
        +----- decision record / frozen task.md -----------------+
        ^                                                        |
        +----- result.json / status.json / diff evidence --------+
```

Executor is always Cursor CLI. Claude.ai web chat alone cannot run the local CLI.

## Prerequisites

- Python 3.10+
- Cursor agent CLI installed and logged in on the machine that runs `handoff.py`
- Optional: Claude Code CLI for `consult.py` when the user requests Claude discussion
- Planner host with skills support (Codex or Claude Code)

This package does **not** include Cursor/Claude credentials, API keys, or model subscriptions.

## Platforms

| Platform | Targeted | Live verification status |
| --- | --- | --- |
| Windows | Yes | Portable Cursor smoke verified on a separate toy workspace (function creation, 2 assertions, `execution_completed`, lock released) |
| Linux | Yes | Not live-verified here; CI matrix intended |
| macOS | Yes | Not live-verified here; CI matrix intended |

Unit tests use fake Cursor/Claude CLIs and do **not** prove Linux/macOS live CLI behavior.

**Claude real advice:** UNVERIFIED — a live consult returned `is_error=true` (weekly quota). Do not treat offline fake-Claude tests as live advice verification.

Installation from an extracted release archive works (`python install.py ...`). Clone quickstart uses `https://github.com/Lunecid/cursor-handoff` then `cd cursor-handoff`.

## Install

Clone or extract a release archive, then install the skill (local copy only; does not install Cursor, Claude, or Python):

```bash
git clone https://github.com/Lunecid/cursor-handoff
cd cursor-handoff
python install.py --target both --scope user
```

Examples:

```bash
# Codex user skills (documented default: ~/.agents/skills)
python install.py --target codex --scope user

# Older Codex hosts that still read ~/.codex/skills
python install.py --target codex --scope user --destination ~/.codex/skills

# Claude Code user skills
python install.py --target claude --scope user

# Project-scoped install
python install.py --target both --scope project --project /path/to/repo

# Replace an existing install (backup under .cursor-handoff-backups/<id>/)
python install.py --target codex --scope user --update
```

Default behavior refuses to overwrite an existing skill directory unless `--update` is set. Updates store a same-filesystem backup under hidden `.cursor-handoff-backups/<unique-id>/` so a `SKILL.md` backup is not discovered as another skill.

## Usage

### Optional Claude discussion (critique only)

Only when the user asks for Claude discussion/review. At most two consult calls by default. If the host **is** Claude Code, do not automatically consult yourself — only run this for an explicit independent review request.

```bash
python skills/cursor-handoff/scripts/consult.py \
  --workspace /path/to/project \
  --brief /path/to/project/design-brief.md
```

Useful flags: `--timeout 300`, `--model`, `--claude-path` / `CLAUDE_CODE_PATH`, `--dry-run`, `--doctor`.

Artifacts land in `.cursor-handoff/consult-<id>/` (`brief.md`, `response.json`, `advice.md`, `stderr.log`, `status.json`, ephemeral settings/MCP config). The Claude process cwd is a runner-owned temp directory **outside** the workspace (cleaned up afterward); home/managed policy may still apply. Success state is **`advice_received`** (not consensus). Record adopted/rejected suggestions before freezing a Cursor task. See `examples/design-brief.md`.

Quota or login failures are coordinator blockers — do not auto-switch models or providers.

### Cursor implementation handoff

1. Agree on a plan in Codex (`$cursor-handoff`) or Claude Code (`/cursor-handoff`), optionally after consult synthesis.
2. Write a UTF-8 task file inside the target workspace (see `examples/task.md`).
3. Dispatch:

To conserve planner tokens, let Cursor explore the code and run validation. The planner reads project rules and enough context to set scope, then reviews Cursor's concise result and the relevant diff. Read full logs only when an error or risk calls for them. Claude consultation is optional and uses Claude tokens when requested; total token or cost savings are not guaranteed.

```bash
python skills/cursor-handoff/scripts/handoff.py \
  --workspace /path/to/project \
  --task /path/to/project/.cursor-handoff-task.md
```

Useful flags:

- `--timeout 600` — process-tree kill deadline
- `--model <id>` — optional Cursor model
- `--agent-path` / `CURSOR_AGENT_PATH` — executable or Windows `agent.ps1`
- `--trust-workspace` — deliberately pass Cursor `--trust` for this exact workspace (default off). May persist a `.workspace-trusted` marker that subdirectories inherit — not a silent per-run-only switch; never auto-enabled
- `--live` — human progress on stderr (workspace, task, trust flag, pid/session, tool summaries, stderr errors, final state); stdout JSON stays compatible
- `--open-terminal` — Windows: open a separate console running `watch.py` before Cursor; other OS: print a manual watch command and enable live progress
- `--dry-run` — validate and print dispatch plan; no runs/locks/mutations
- `--doctor` — installation/capability report; no login or config writes

Visible watch (observation only; cannot answer trust prompts):

```bash
# Windows: live progress + separate viewer console
python skills/cursor-handoff/scripts/handoff.py \
  --workspace /path/to/project \
  --task /path/to/project/.cursor-handoff-task.md \
  --live --open-terminal

# Linux/macOS: live progress; optional manual viewer
python skills/cursor-handoff/scripts/handoff.py \
  --workspace /path/to/project \
  --task /path/to/project/.cursor-handoff-task.md \
  --live
python skills/cursor-handoff/scripts/watch.py --run /path/to/project/.cursor-handoff/<run-id>
```

Artifacts land in `.cursor-handoff/<run-id>/` (`task.md`, `events.jsonl`, `stderr.log`, `result.json`, `status.json`). The runner points Cursor at the frozen `run/task.md` snapshot. Treat logs as private. A CLI `execution_completed` state means the agent finished with a supported success result (`type=result`, `subtype=success`, `is_error=false`); it is **not** automatic review acceptance. Explicit workspace-trust denial becomes `blocked` / `trust_required` (nonzero exit). If consent was already rejected, do not bypass it; if trust was never configured, deliberately re-run with `--trust-workspace` for that exact workspace (Cursor may persist `.workspace-trusted`) or complete interactive Cursor trust setup. The runner never auto-enables `--trust`.

## Offline checks

```bash
python skills/cursor-handoff/scripts/handoff.py --doctor
python skills/cursor-handoff/scripts/consult.py --doctor
python skills/cursor-handoff/scripts/handoff.py --workspace . --task examples/task.md --dry-run
python skills/cursor-handoff/scripts/consult.py --workspace . --brief examples/design-brief.md --dry-run
```

## Validation

```bash
python -m unittest discover -s tests -v
```

Default tests use mocked launchers and never open a real console window. The real Windows GUI handshake (`test_windows_viewer_console_handshake`) is opt-in:

```powershell
# Windows only; opens a CREATE_NEW_CONSOLE viewer window
$env:CURSOR_HANDOFF_TEST_GUI = '1'
python -m unittest discover -s tests -p test_handoff.py -k test_windows_viewer_console_handshake -v
```

## Package a release

```bash
python scripts/package_release.py
```

Creates deterministic `dist/cursor-handoff-<version>.zip` and `.sha256`. Extracted archives can run `python install.py ...` directly.

## Troubleshooting

| Symptom | What to try |
| --- | --- |
| Agent not found / stale PATH | `--doctor`, `--agent-path`, or `CURSOR_AGENT_PATH`; on Windows check `%LOCALAPPDATA%\cursor-agent\agent.ps1` |
| Claude CLI not found | `consult.py --doctor`, `--claude-path`, or `CLAUDE_CODE_PATH`; prefer a native `.exe` on Windows |
| Trust / permission prompts | Pass `--trust-workspace` only when intentional (may persist `.workspace-trusted`); workspace path is not an OS sandbox. Viewer cannot answer trust prompts. `blocked`/`trust_required` means explicit denial — do not auto-retry or bypass rejected consent |
| Login / quota required | Run the relevant CLI login on the host; do not bypass or auto-switch providers |
| Timeout | Raise `--timeout`; inspect `stderr.log` and `status.json` (`timeout` is retained) |
| Lock exists | Another handoff/consult may be running, or a stale `.cursor-handoff/workspace.lock` remains — confirm no runner, then remove the lock manually |

## Official docs

- [Cursor CLI headless](https://cursor.com/docs/cli/headless)
- [Cursor CLI parameters](https://cursor.com/docs/cli/reference/parameters)
- [Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference)
- [Claude Code headless](https://code.claude.com/docs/en/headless)
- [Claude Code skills](https://code.claude.com/docs/en/skills)
- [ChatGPT/Codex skills](https://learn.chatgpt.com/docs/build-skills)

## License

MIT — see `LICENSE`.
