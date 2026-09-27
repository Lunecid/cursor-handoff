#!/usr/bin/env python3
"""Install the cursor-handoff skill into Codex and/or Claude Code skill directories.

Update backups are stored under a hidden sibling directory
`.cursor-handoff-backups/<unique-id>/` (same filesystem parent as the skill)
so a SKILL.md inside the skills tree does not create a duplicate discoverable skill.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
from pathlib import Path
import shutil
import sys
import uuid

SKILL_NAME = "cursor-handoff"
BACKUP_ROOT_NAME = ".cursor-handoff-backups"
SKIP_DIR_NAMES = {
    "__pycache__",
    ".git",
    ".cursor-handoff",
    ".handoff-dev",
    "dist",
    ".pytest_cache",
}
SKIP_FILE_SUFFIXES = {".pyc", ".pyo"}
SKIP_FILE_NAMES = {".DS_Store"}


class InstallError(Exception):
    pass


def repo_root() -> Path:
    return Path(__file__).resolve().parent


def skill_source() -> Path:
    return repo_root() / "skills" / SKILL_NAME


def default_dest(target: str, scope: str, project: Path | None) -> Path:
    if target == "codex":
        if scope == "user":
            return Path.home() / ".agents" / "skills" / SKILL_NAME
        if project is None:
            raise InstallError("--project is required for --scope project")
        return project.resolve() / ".agents" / "skills" / SKILL_NAME
    if target == "claude":
        if scope == "user":
            return Path.home() / ".claude" / "skills" / SKILL_NAME
        if project is None:
            raise InstallError("--project is required for --scope project")
        return project.resolve() / ".claude" / "skills" / SKILL_NAME
    raise InstallError(f"Unknown target: {target}")


def path_has_symlink_ancestor(path: Path) -> Path | None:
    """Return the first symlink/junction in path or its ancestry, before resolve hides it."""
    current = path
    seen: set[str] = set()
    while True:
        key = str(current)
        if key in seen:
            break
        seen.add(key)
        try:
            if current.is_symlink():
                return current
        except OSError:
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def assert_safe_final_destination(path: Path) -> None:
    """Reject broken symlink finals and symlink/junction ancestry before resolve."""
    ancestor = path_has_symlink_ancestor(path)
    if ancestor is not None:
        raise InstallError(
            f"Refusing symlink/junction in destination ancestry: {ancestor}. "
            "Pass a canonical path (for example Path(...).resolve()) so platform temp "
            "redirects such as macOS /var -> /private/var are not treated as user-created "
            "unsafe links."
        )
    if path.exists() or path.is_symlink():
        if path.is_symlink():
            try:
                if not path.exists():
                    raise InstallError(f"Refusing broken symlink destination: {path}")
            except OSError as exc:
                raise InstallError(f"Refusing unreadable symlink destination: {path} ({exc})") from exc
            raise InstallError(f"Refusing symlink destination: {path}")
        if path.is_dir():
            try:
                for child in path.rglob("*"):
                    if child.is_symlink():
                        raise InstallError(f"Refusing destination containing symlink: {child}")
            except OSError as exc:
                raise InstallError(f"Failed inspecting destination {path}: {exc}") from exc
        elif not path.is_dir():
            # exists and not dir handled by caller
            pass


def resolve_destination(
    target: str,
    scope: str,
    project: Path | None,
    destination: Path | None,
) -> Path:
    if destination is not None:
        parent = destination.expanduser()
        ancestor = path_has_symlink_ancestor(parent)
        if ancestor is not None:
            raise InstallError(
                f"Refusing symlink/junction in --destination ancestry: {ancestor}. "
                "Pass a canonical --destination path (for example Path(...).resolve()) so "
                "platform temp redirects such as macOS /var -> /private/var are not treated "
                "as user-created unsafe links."
            )
        try:
            return parent.resolve() / SKILL_NAME
        except OSError as exc:
            raise InstallError(f"Cannot resolve destination {parent}: {exc}") from exc
    return default_dest(target, scope, project)


def should_skip(path: Path, root: Path) -> bool:
    rel_parts = path.relative_to(root).parts
    if any(part in SKIP_DIR_NAMES for part in rel_parts[:-1]):
        return True
    if path.is_dir() and path.name in SKIP_DIR_NAMES:
        return True
    if path.is_file():
        if path.name in SKIP_FILE_NAMES or path.suffix in SKIP_FILE_SUFFIXES:
            return True
        if path.name.endswith(".log"):
            return True
    return False


def iter_source_files(src: Path) -> list[Path]:
    files: list[Path] = []
    try:
        for path in sorted(src.rglob("*")):
            if should_skip(path, src):
                continue
            if path.is_symlink():
                raise InstallError(f"Refusing to install symlink source: {path}")
            if path.is_file():
                files.append(path)
    except OSError as exc:
        raise InstallError(f"Failed reading skill source {src}: {exc}") from exc
    return files


def validate_source(src: Path) -> None:
    if not src.is_dir():
        raise InstallError(f"Skill source missing or not a directory: {src}")
    if src.is_symlink():
        raise InstallError(f"Refusing symlink skill source: {src}")
    ancestor = path_has_symlink_ancestor(src)
    if ancestor is not None:
        raise InstallError(f"Refusing symlink/junction in skill source ancestry: {ancestor}")
    if not (src / "SKILL.md").is_file():
        raise InstallError(f"Skill source missing SKILL.md: {src}")
    # Force enumeration early so copy surprises become preflight failures.
    files = iter_source_files(src)
    if not files:
        raise InstallError(f"No installable files under {src}")


def unique_backup_id() -> str:
    # Microsecond stamp plus UUID keeps same-filesystem uniqueness without collisions.
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S%f")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def backup_existing(dest: Path) -> Path:
    parent = dest.parent
    backup_root = parent / BACKUP_ROOT_NAME
    try:
        backup_root.mkdir(parents=True, exist_ok=True)
        if backup_root.is_symlink():
            raise InstallError(f"Refusing symlink backup root: {backup_root}")
        backup = backup_root / unique_backup_id() / dest.name
        backup.parent.mkdir(parents=True, exist_ok=False)
        os.rename(dest, backup)
    except InstallError:
        raise
    except OSError as exc:
        raise InstallError(f"Failed creating backup for {dest}: {exc}") from exc
    return backup


def copy_skill(src: Path, dest: Path) -> None:
    files = iter_source_files(src)
    if not files:
        raise InstallError(f"No installable files under {src}")
    try:
        dest.mkdir(parents=True, exist_ok=False)
        for file_path in files:
            rel = file_path.relative_to(src)
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file_path, target)
    except OSError as exc:
        raise InstallError(f"Failed copying skill to {dest}: {exc}") from exc


def quarantine_partial(dest: Path) -> Path | None:
    """Move a partial install aside for diagnosis; do not leave it as the active skill."""
    if not dest.exists() and not dest.is_symlink():
        return None
    failed = dest.parent / f"{dest.name}.failed.{unique_backup_id()}"
    try:
        os.rename(dest, failed)
    except OSError as exc:
        raise InstallError(
            f"Partial install at {dest} could not be quarantined ({exc}). "
            "Manual cleanup may be required; the incomplete tree was not left renamed."
        ) from exc
    return failed


def install_one(src: Path, dest: Path, update: bool) -> dict:
    assert_safe_final_destination(dest)
    result = {"destination": str(dest), "updated": False, "backup": None, "partial": None}
    if dest.exists():
        if not update:
            raise InstallError(
                f"Destination already exists: {dest}. Pass --update to backup and replace."
            )
        backup = backup_existing(dest)
        result["backup"] = str(backup)
        result["updated"] = True
        try:
            copy_skill(src, dest)
        except Exception:
            partial = quarantine_partial(dest)
            result["partial"] = str(partial) if partial else None
            try:
                os.rename(backup, dest)
            except OSError as restore_exc:
                raise InstallError(
                    f"Update copy failed and restore from backup also failed: {restore_exc}. "
                    f"Backup remains at {backup}."
                ) from restore_exc
            raise
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        ancestor = path_has_symlink_ancestor(dest.parent)
        if ancestor is not None:
            raise InstallError(f"Refusing symlink/junction parent: {ancestor}")
        try:
            copy_skill(src, dest)
        except Exception:
            partial = quarantine_partial(dest)
            result["partial"] = str(partial) if partial else None
            raise
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        choices=("codex", "claude", "both"),
        required=True,
        help="Install for Codex, Claude Code, or both",
    )
    parser.add_argument(
        "--scope",
        choices=("user", "project"),
        required=True,
        help="User-level or project-level skill directory",
    )
    parser.add_argument(
        "--project",
        type=Path,
        help="Project root for --scope project",
    )
    parser.add_argument(
        "--destination",
        type=Path,
        help=(
            "Explicit parent directory override for a single target "
            "(e.g. ~/.codex/skills for older Codex hosts). Not valid with --target both."
        ),
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help=(
            "Backup existing skill under .cursor-handoff-backups/<id>/ "
            "(same parent filesystem), then replace"
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    # Prefer UTF-8 console output on Windows cp949/cp1252 hosts when supported.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError, AttributeError):
                pass
    args = parse_args(argv)
    try:
        src = skill_source()
        validate_source(src)
        if args.destination is not None and args.target == "both":
            print("--destination requires a single --target (codex or claude)", file=sys.stderr)
            return 1
        if args.scope == "project" and args.project is None and args.destination is None:
            print(
                "--project is required for --scope project unless --destination is set",
                file=sys.stderr,
            )
            return 1

        targets = ["codex", "claude"] if args.target == "both" else [args.target]
        planned: list[tuple[str, Path]] = []
        for target in targets:
            dest = resolve_destination(target, args.scope, args.project, args.destination)
            planned.append((target, dest))
        # Preflight all destinations before mutating any.
        for _target, dest in planned:
            assert_safe_final_destination(dest)
            if dest.exists() and not args.update:
                raise InstallError(
                    f"Destination already exists: {dest}. Pass --update to backup and replace."
                )
            if dest.exists() and not dest.is_dir():
                raise InstallError(f"Destination exists and is not a directory: {dest}")

        results = []
        for target, dest in planned:
            results.append({"target": target, **install_one(src, dest, args.update)})
    except InstallError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Install failed due to OS error: {exc}", file=sys.stderr)
        return 1

    for item in results:
        print(f"Installed {item['target']} -> {item['destination']}")
        if item.get("backup"):
            print(f"  backup: {item['backup']}")
        if item.get("partial"):
            print(f"  partial (quarantined): {item['partial']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
