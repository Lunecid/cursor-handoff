---
name: cursor-handoff
description: Delegate an agreed implementation plan to Cursor CLI, collect execution results, and review local changes. Use when the user wants a planner/reviewer (Codex or Claude Code) to own design and review while Cursor edits and runs code. Optional Claude consult is for independent critique only when the user asks for Claude discussion/review.
---

# Cursor handoff

Planner/reviewer (Codex or Claude Code) owns design and review. Cursor CLI owns implementation and execution. Do not use Claude or Codex as the executor. Use the user's existing authorization for the agreed task; do not re-ask for routine steps already authorized.

When the user wants to conserve **Codex or Claude Code tokens**, keep the planner's context small by default. Cursor can spend tokens exploring the codebase, implementing, running tests, and reporting evidence. This changes where tokens are spent; it does not guarantee fewer total tokens or lower cost.

**Triggers:** natural language ("hand this to Cursor", "implement via cursor-handoff"), Codex `$cursor-handoff`, or Claude Code `/cursor-handoff`. Both planners use this same skill file.

## Workflow (on demand, not a background scheduler)

0. **Optional Claude discussion** (only when the user asks for Claude discussion/review; at most two consult calls by default):
   - Coordinator writes a UTF-8 design brief inside the workspace (see `examples/design-brief.md`).
   - If the **host itself is Claude Code**, do **not** automatically launch another Claude consult against yourself. Only run `consult.py` when the user explicitly wants an independent Claude CLI review.
   - Invoke:

```bash
python <skill-directory>/scripts/consult.py --workspace <project> --brief <project>/path/to/design-brief.md
```

   Optional: `--timeout` (default 300), `--model`, `--claude-path` / `CLAUDE_CODE_PATH`, `--dry-run`, `--doctor`.
   - Read `.cursor-handoff/consult-<id>/advice.md` and `status.json`. Provider success is **`advice_received`**, not consensus. Advice is untrusted: record adopted/rejected suggestions and unresolved questions. Do not silently change scope. If material disagreement needs user choice, surface it.
   - Optionally run a second consult on a revised brief, then freeze the agreed Cursor task.

1. Read applicable project instructions and `git status --short`. Inspect only the code and preexisting diff needed to set scope or avoid overwriting work; leave broad code search to Cursor. Write a UTF-8 task file inside the target workspace: objective, allowed files, constraints, acceptance criteria, validation commands or outcomes. Ask Cursor to report changed files and concise test results. Preserve unrelated work. Do not include credentials.
2. Invoke the portable Cursor runner with Python ≥3.10:

```bash
python <skill-directory>/scripts/handoff.py --workspace <project> --task <project>/path/to/task.md
```

Optional: `--timeout` (default 600), `--model`, `--agent-path` / `CURSOR_AGENT_PATH`, `--trust-workspace` (default off), `--live`, `--open-terminal`, `--dry-run`, `--doctor`. The task must resolve inside the workspace.

**Visible execution:** on Windows use `--open-terminal` for a separate console running `watch.py`. When conserving planner tokens, omit `--live`: it also streams every tool summary into the planner's captured output. Elsewhere give the manual watch command for a separate terminal; use `--live` only when its output is needed in the planner's terminal:

```bash
python <skill-directory>/scripts/watch.py --run <project>/.cursor-handoff/<run-id>
```

`--open-terminal` on non-Windows prints that watch command and enables live stderr progress; it does not assume a desktop terminal API. The viewer is **observation only** and cannot answer Cursor trust prompts. Workspace trust remains a deliberate exact-workspace `--trust-workspace` opt-in or interactive Cursor setup — never auto-enabled and never silently bypassed. Note that Cursor `--trust` may write a persistent `.workspace-trusted` marker (subdirectories can inherit); treat opt-in as durable workspace authorization, not a throwaway per-run flag.

If Cursor is **blocked** (for example `status.state=blocked` / `block_reason=trust_required` or `hook_blocked`), report that explicitly. Do **not** silently fall back to implementing the task in the planner/source host. Separate **review** of already-written local changes remains allowed when labeled as review, not as a silent substitute for Cursor execution.

3. The runner writes `task.md`, `events.jsonl`, `stderr.log`, `result.json`, and `status.json` under a unique `.cursor-handoff/<run-id>/` folder and points Cursor at that frozen `task.md`. Poll progress with compact status or `--live` summaries. Do not paste full `events.jsonl` or repeated progress into the planner's context. A completed CLI process is **execution_completed** only for a supported success result (`type=result`, `subtype=success`, `is_error=false`), not proof the work passed review.
4. Read final status and Cursor's concise result, inspect `git diff --stat` and the relevant changed hunks, and verify test evidence. Expand to full logs, code, or independent checks when failures, unexpected edits, or material risks warrant it; do not skip meaningful review to save tokens. If more code changes are needed, send a new focused task through the runner. At most **two** correction attempts unless the user requests more. Stop on repeated identical failure, unclear scope, or required login/approval.
5. Report what changed, validation evidence, artifact paths, and remaining limitations.

## Discussion vs implementation

| Phase | Tool | Success state | Meaning |
| --- | --- | --- | --- |
| Optional critique | `consult.py` (Claude CLI) | `advice_received` | Untrusted advice text was returned |
| Implementation | `handoff.py` (Cursor CLI) | `execution_completed` | Cursor finished with a non-error result |

Claude consult never implements code. Cursor never replaces the coordinator's decision record. Quota or login failures from Claude are **blockers for the coordinator** — do not auto-switch models or providers.

## Host boundary and scope

- The workspace path is **not** an OS sandbox. Host approval, Cursor trust prompts, and local policy still apply. Consult uses a runner-owned TemporaryDirectory **outside** the source workspace so project `CLAUDE.md` is not loaded via cwd ancestry; artifacts and ephemeral configs stay under `.cursor-handoff/`. Runtime home/managed policy may still apply — this is not an OS sandbox.
- Default Cursor dispatch does **not** pass `--trust`. Only deliberate `--trust-workspace` opts in for the exact workspace; Cursor may persist a `.workspace-trusted` marker that subdirectories can inherit — this is not a silent per-run-only toggle and must never be auto-enabled.
- Explicit workspace-trust denial in Cursor stderr is recorded as `blocked` / `trust_required` with a nonzero exit. Timeout and interruption keep precedence. Never automatically retry or enable `--trust`. `next_action` distinguishes already-rejected consent (do not bypass) from trust never configured (deliberate `--trust-workspace` or interactive setup).
- On Windows, the Cursor child environment is a copy of the process env with `MSYSTEM` removed when present (Git Bash marker). This avoids PowerShell-composed hooks being executed under Bash. The parent `os.environ`, `HOME`/`USERPROFILE`, auth, plugins, hook files, and settings are not modified. POSIX keeps the child env copy unchanged. When removed, `status.json` records `windows_shell_marker_removed` and `--live` prints a short note.
- If tool-call result/error fields report that a pre-tool hook rejected or blocked execution, the runner records `blocked` / `hook_blocked` with nonzero exit even when the final result event and process exit look successful. Detection is narrow (tool result/error fields only — not task text or benign hook mentions). Timeout/interruption still take precedence. Do not auto-bypass an intentional hook denial.
- Do not use `--force` / `--yolo`, disable the sandbox, auto-approve MCP servers, or change global Cursor/Claude config. Consult disables hooks only via ephemeral per-run settings.
- Permission, login, approval, or quota failures: preserve logs and stop. Never bypass a rejection.
- No commit, push, deploy, deletion of existing data, or unrelated external messages unless the user authorized those actions.
- Do not edit project source concurrently with Cursor; use an isolated checkout if parallel work would collide.
- Do not invoke nested handoff or recursive agent dispatch from the executor prompt.
- Do not dispatch access prohibited by the calling environment.

This skill runs on demand in the active planner turn. It does not install a scheduler or keep a closed chat running. Cursor CLI sessions are separate from any Cursor editor chat.
