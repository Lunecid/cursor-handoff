# Example design brief (Claude consult)

Use this as the `--brief` input for an optional Claude Code design consult before
freezing a Cursor handoff task. Claude advice is untrusted input: record what you
adopt, reject, or leave unresolved. Do not treat a successful CLI exit as agreement.

## Objective

Add an optional Claude consult step to cursor-handoff so a coordinator can get an
independent critique of a draft plan before dispatching Cursor as the sole executor.

## Options under consideration

1. **Coordinator-only synthesis** (recommended default): Claude returns critique text;
   the coordinator writes a decision record and freezes the Cursor task.
2. **Auto-merge Claude suggestions into the task**: faster, but risks silently changing
   scope from untrusted advice.
3. **Skip consult**: proceed directly to Cursor when the user did not ask for Claude
   discussion.

## Constraints

- Cursor remains the only implementation executor.
- At most two consult calls by default.
- No credentials in the brief; no repository crawl from the consult runner.
- Quota/login failures are blockers for the coordinator — do not auto-switch models.
- Offline tests use a fake Claude CLI only.

## Open questions for the reviewer

1. Should consult share the handoff workspace lock, or only protect symlink run dirs?
2. What brief size limit is appropriate for stdin (proposal: 128 KiB)?
3. Which ephemeral settings keys are required to disable hooks without touching global config?

## Suggested decision record (coordinator fills after advice)

```markdown
## Decision record

- Consult run: `.cursor-handoff/consult-<id>/`
- Advice status: advice_received | failed | timeout | skipped
- Adopted:
  - ...
- Rejected:
  - ...
- Unresolved / needs user choice:
  - ...
- Frozen Cursor task path:
  - ...
```
