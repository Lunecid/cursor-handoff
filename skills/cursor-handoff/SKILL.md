---
name: cursor-handoff
description: Delegate an agreed implementation plan to Cursor CLI, collect execution results, and review local changes. Use when the user wants a planner/reviewer (Codex or Claude Code) to own design and review while Cursor edits and runs code. Optional Claude consult is for independent critique only when the user asks for Claude discussion/review.
---

# Cursor handoff

Planner/reviewer (Codex or Claude Code) owns design and review. Cursor CLI owns implementation and execution. Do not use Claude or Codex as the executor. Use the user's existing authorization for the agreed task; do not re-ask for routine steps already authorized.

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

1. Inspect project instructions and preexisting changes. Write a UTF-8 task file inside the target workspace: objective, allowed files, constraints, acceptance criteria, exact validation commands. Preserve unrelated work. Do not include credentials. For project changes, record `git status --short` and a relevant diff before dispatch.
2. Invoke the portable Cursor runner with Python ≥3.10:

```bash
python <skill-directory>/scripts/handoff.py --workspace <project> --task <project>/path/to/task.md
```

Optional: `--timeout` (default 600), `--model`, `--agent-path` / `CURSOR_AGENT_PATH`, `--trust-workspace` (default off), `--dry-run`, `--doctor`. The task must resolve inside the workspace.

3. The runner writes `task.md`, `events.jsonl`, `stderr.log`, `result.json`, and `status.json` under a unique `.cursor-handoff/<run-id>/` folder and points Cursor at that frozen `task.md`. Poll progress while waiting. A completed CLI process is **execution_completed** only for a supported success result (`type=result`, `subtype=success`, `is_error=false`), not proof the work passed review.
4. Read the result event, inspect the actual diff against the pre-dispatch state, and verify test evidence. The planner may perform read-only checks. If more code changes are needed, send a new focused task through the runner. At most **two** correction attempts unless the user requests more. Stop on repeated identical failure, unclear scope, or required login/approval.
5. Report what changed, validation evidence, artifact paths, and remaining limitations.

## Discussion vs implementation

| Phase | Tool | Success state | Meaning |
| --- | --- | --- | --- |
| Optional critique | `consult.py` (Claude CLI) | `advice_received` | Untrusted advice text was returned |
| Implementation | `handoff.py` (Cursor CLI) | `execution_completed` | Cursor finished with a non-error result |

Claude consult never implements code. Cursor never replaces the coordinator's decision record. Quota or login failures from Claude are **blockers for the coordinator** — do not auto-switch models or providers.

## Host boundary and scope

- The workspace path is **not** an OS sandbox. Host approval, Cursor trust prompts, and local policy still apply. Consult uses a runner-owned TemporaryDirectory **outside** the source workspace so project `CLAUDE.md` is not loaded via cwd ancestry; artifacts and ephemeral configs stay under `.cursor-handoff/`. Runtime home/managed policy may still apply — this is not an OS sandbox.
- Default Cursor dispatch does **not** pass `--trust`. Only `--trust-workspace` opts in for that run.
- Do not use `--force` / `--yolo`, disable the sandbox, auto-approve MCP servers, or change global Cursor/Claude config. Consult disables hooks only via ephemeral per-run settings.
- Permission, login, approval, or quota failures: preserve logs and stop. Never bypass a rejection.
- No commit, push, deploy, deletion of existing data, or unrelated external messages unless the user authorized those actions.
- Do not edit project source concurrently with Cursor; use an isolated checkout if parallel work would collide.
- Do not invoke nested handoff or recursive agent dispatch from the executor prompt.
- Do not dispatch access prohibited by the calling environment.

This skill runs on demand in the active planner turn. It does not install a scheduler or keep a closed chat running. Cursor CLI sessions are separate from any Cursor editor chat.
