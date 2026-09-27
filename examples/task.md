# Example handoff task

## Objective
Create a file named `hello.txt` in the workspace root containing exactly:

```
hello from cursor-handoff
```

## Constraints
- Do not modify any other files.
- Do not commit, push, or access credentials.

## Acceptance
- `hello.txt` exists at the workspace root.
- File contents match the line above (UTF-8, optional trailing newline).

## Validation
```bash
python -c "from pathlib import Path; t=Path('hello.txt').read_text(encoding='utf-8').strip(); assert t=='hello from cursor-handoff', t"
```
