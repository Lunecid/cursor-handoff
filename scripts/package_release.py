#!/usr/bin/env python3
"""Create a deterministic release zip and SHA256 digest for cursor-handoff."""

from __future__ import annotations

import hashlib
from pathlib import Path
import re
import sys
import zipfile

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
VERSION_FILE = ROOT / "VERSION"
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)

# Explicit allowlist only. Never pack .cursor-handoff, .handoff-dev, logs, or credentials.
ALLOWLIST = [
    "skills/cursor-handoff/SKILL.md",
    "skills/cursor-handoff/scripts/handoff.py",
    "skills/cursor-handoff/scripts/consult.py",
    "install.py",
    "scripts/package_release.py",
    "README.md",
    "README.ko.md",
    "LICENSE",
    "VERSION",
    "examples/task.md",
    "examples/design-brief.md",
    ".gitignore",
    ".gitattributes",
]


def read_version() -> str:
    text = VERSION_FILE.read_text(encoding="utf-8").strip()
    if not text:
        raise SystemExit("VERSION is empty")
    if ".." in text or "/" in text or "\\" in text or text.startswith("."):
        raise SystemExit(f"VERSION must be a plain semver, not a path: {text!r}")
    if not SEMVER_RE.match(text):
        raise SystemExit(f"VERSION is not valid semver: {text!r}")
    return text


def path_has_symlink_ancestor(path: Path) -> Path | None:
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


def collect_files() -> list[tuple[str, Path]]:
    items: list[tuple[str, Path]] = []
    for rel in ALLOWLIST:
        path = ROOT / rel
        if not path.is_file() and not path.is_symlink():
            raise SystemExit(f"Missing required distribution file: {rel}")
        if path.is_symlink():
            raise SystemExit(f"Refusing symlink source: {rel}")
        ancestor = path_has_symlink_ancestor(path)
        if ancestor is not None:
            raise SystemExit(
                f"Refusing symlink ancestor in source path {rel}: {ancestor}. "
                "Use canonical source paths (Path.resolve()); platform temp redirects "
                "such as macOS /var -> /private/var are not user-created package links."
            )
        # Ensure the resolved file stays under ROOT (escaping links via ancestors).
        try:
            resolved = path.resolve()
            resolved.relative_to(ROOT.resolve())
        except (ValueError, OSError) as exc:
            raise SystemExit(f"Source path escapes package root: {rel} ({exc})") from exc
        items.append((rel.replace("\\", "/"), path))
    return items


def fixed_time() -> tuple[int, int, int, int, int, int]:
    # Deterministic timestamp for reproducible archives.
    return (2026, 1, 1, 0, 0, 0)


def normalize_payload(path: Path) -> bytes:
    data = path.read_bytes()
    name = path.name
    suffix = path.suffix.lower()
    if name in {".gitignore", ".gitattributes"} or suffix in {".md", ".py", ".txt", ".yml", ".yaml"} or name == "VERSION" or name == "LICENSE":
        text = data.decode("utf-8")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if text and not text.endswith("\n"):
            text += "\n"
        return text.encode("utf-8")
    return data


def write_zip(version: str, files: list[tuple[str, Path]]) -> Path:
    DIST.mkdir(parents=True, exist_ok=True)
    out = DIST / f"cursor-handoff-{version}.zip"
    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for arcname, path in sorted(files, key=lambda item: item[0]):
            info = zipfile.ZipInfo(arcname)
            info.date_time = fixed_time()
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 0  # fat/msdos for cross-platform reproducibility
            info.create_version = 20
            info.extract_version = 20
            info.external_attr = 0o644 << 16
            info.flag_bits = 0
            data = normalize_payload(path)
            zf.writestr(info, data)
    return out


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    version = read_version()
    # Construct output path only after semver validation.
    files = collect_files()
    archive = write_zip(version, files)
    digest = sha256_file(archive)
    digest_path = DIST / f"cursor-handoff-{version}.zip.sha256"
    digest_path.write_text(f"{digest}  {archive.name}\n", encoding="utf-8", newline="\n")
    print(archive.name)
    print(digest_path.name)
    print(f"sha256={digest}")
    print(f"files={len(files)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
