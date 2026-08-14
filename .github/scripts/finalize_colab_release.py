"""Create immutable Colab tags after an approved portal PR is merged.

Install this file in student-lab at .github/scripts/finalize_colab_release.py.
It intentionally accepts only checked-in student-release-v1 manifests changed by
the merge and validates every declared public byte before creating any tag.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path, PurePosixPath


TAG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{5,100}$")


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "core.hooksPath=", *args],
        check=check,
        text=True,
        capture_output=True,
        timeout=60,
    )


def safe_file(repository: Path, value: object) -> Path:
    if not isinstance(value, str) or "\\" in value or any(ord(char) < 32 for char in value):
        raise ValueError("Release manifest contains an unsafe path.")
    relative = PurePosixPath(value)
    if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("Release manifest contains an unsafe path.")
    candidate = (repository / Path(*relative.parts)).resolve()
    candidate.relative_to(repository)
    if not candidate.is_file() or candidate.is_symlink():
        raise ValueError(f"Declared release file is missing or unsafe: {value}")
    return candidate


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_declared_file(repository: Path, record: object) -> None:
    if not isinstance(record, dict):
        raise ValueError("Release manifest contains an invalid file record.")
    path = safe_file(repository, record.get("path"))
    if path.stat().st_size != record.get("bytes") or sha256(path) != record.get("sha256"):
        raise ValueError(f"Released bytes do not match the manifest: {record.get('path')}")


def changed_release_manifests(repository: Path, base: str, commit: str) -> list[Path]:
    changed = git("diff", "--name-only", "--diff-filter=AM", base, commit, "--").stdout.splitlines()
    manifests = []
    for value in changed:
        relative = PurePosixPath(value)
        if relative.name != "release-manifest.json":
            continue
        manifests.append(safe_file(repository, value))
    return manifests


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--repository", required=True)
    args = parser.parse_args()

    repository = Path.cwd().resolve()
    if git("rev-parse", "HEAD").stdout.strip() != args.commit:
        raise SystemExit("Checked-out commit does not match the merged PR commit.")

    tags: list[str] = []
    for manifest_path in changed_release_manifests(repository, args.base, args.commit):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("schemaVersion") != "student-release-v1":
            raise SystemExit(f"Unsupported release manifest: {manifest_path}")
        if manifest.get("repository") != args.repository:
            raise SystemExit(f"Release repository mismatch: {manifest_path}")
        tag = manifest.get("immutableRef")
        if not isinstance(tag, str) or not TAG_PATTERN.fullmatch(tag):
            raise SystemExit(f"Unsafe immutable release tag: {tag!r}")
        if tag in tags:
            raise SystemExit(f"Duplicate immutable release tag in one merge: {tag}")
        public_root = PurePosixPath(str(manifest.get("publicRoot", "")))
        manifest_relative = PurePosixPath(manifest_path.relative_to(repository).as_posix())
        if (
            public_root in {PurePosixPath(""), PurePosixPath(".")}
            or public_root.is_absolute()
            or ".." in public_root.parts
            or manifest_relative.parent != public_root
        ):
            raise SystemExit(f"Unsafe public root: {public_root}")
        declared = [manifest.get("notebook"), *manifest.get("resources", [])]
        for record in declared:
            validate_declared_file(repository, record)
            declared_path = PurePosixPath(str(record.get("path", "")))
            if public_root not in declared_path.parents:
                raise SystemExit(f"Declared file escapes the class release root: {declared_path}")
        existing = git("ls-remote", "--tags", "origin", f"refs/tags/{tag}").stdout.strip()
        if existing:
            raise SystemExit(f"Immutable release tag already exists and will not be moved: {tag}")
        tags.append(tag)

    if not tags:
        print("No new student-release-v1 manifest was merged; no Colab tag is required.")
        return 0
    for tag in tags:
        git("tag", tag, args.commit)
    git("push", "--atomic", "origin", *[f"refs/tags/{tag}" for tag in tags])
    for tag in tags:
        remote = git("ls-remote", "--tags", "origin", f"refs/tags/{tag}").stdout.strip()
        if not remote.startswith(args.commit):
            raise SystemExit(f"Remote tag verification failed: {tag}")
        print(f"Created immutable Colab release tag {tag} at {args.commit}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
