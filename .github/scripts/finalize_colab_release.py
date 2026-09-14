"""Create immutable student-data tags after an approved portal PR is merged.

Install this file in student-lab at .github/scripts/finalize_colab_release.py.
It accepts current data-only manifests and legacy notebook manifests changed by
the merge, validates every declared public byte before creating any tag, and
recovers approved manifests whose tag was missed by an older finalizer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import time
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


def release_manifests(repository: Path) -> list[Path]:
    return sorted(
        candidate
        for candidate in repository.rglob("release-manifest.json")
        if candidate.is_file() and not candidate.is_symlink() and ".git" not in candidate.parts
    )


def remote_tag_commits() -> dict[str, str]:
    """Read the remote tag table once, tolerating a short SSH interruption."""
    failure = ""
    for attempt in range(3):
        result = git("ls-remote", "--tags", "origin", check=False)
        if result.returncode == 0:
            direct: dict[str, str] = {}
            peeled: dict[str, str] = {}
            for line in result.stdout.splitlines():
                fields = line.split()
                if len(fields) != 2 or not fields[1].startswith("refs/tags/"):
                    continue
                name = fields[1][len("refs/tags/") :]
                if name.endswith("^{}"):
                    peeled[name[:-3]] = fields[0]
                else:
                    direct[name] = fields[0]
            return {tag: peeled.get(tag, commit) for tag, commit in direct.items()}
        failure = result.stderr.strip() or f"git exited with status {result.returncode}"
        if attempt < 2:
            time.sleep(1)
    raise SystemExit(f"Could not read Student Lab release tags from origin: {failure}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--repository", required=True)
    args = parser.parse_args()

    repository = Path.cwd().resolve()
    if git("rev-parse", "HEAD").stdout.strip() != args.commit:
        raise SystemExit("Checked-out commit does not match the merged PR commit.")

    changed_manifests = set(changed_release_manifests(repository, args.base, args.commit))
    remote_tags = remote_tag_commits()
    tags: list[str] = []
    seen_tags: set[str] = set()
    for manifest_path in release_manifests(repository):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        schema_version = manifest.get("schemaVersion")
        if schema_version not in {"student-data-release-v1", "student-release-v1"}:
            raise SystemExit(f"Unsupported release manifest: {manifest_path}")
        if manifest.get("repository") != args.repository:
            raise SystemExit(f"Release repository mismatch: {manifest_path}")
        tag = manifest.get("immutableRef")
        if not isinstance(tag, str) or not TAG_PATTERN.fullmatch(tag):
            raise SystemExit(f"Unsafe immutable release tag: {tag!r}")
        if tag in seen_tags:
            raise SystemExit(f"Duplicate immutable release tag: {tag}")
        seen_tags.add(tag)
        existing_commit = remote_tags.get(tag)
        if existing_commit:
            if manifest_path in changed_manifests and existing_commit != args.commit:
                raise SystemExit(f"Immutable release tag already exists and will not be moved: {tag}")
            if existing_commit == args.commit:
                print(f"Immutable student-data release tag already exists at this commit: {tag}.")
            continue
        public_root = PurePosixPath(str(manifest.get("publicRoot", "")))
        manifest_relative = PurePosixPath(manifest_path.relative_to(repository).as_posix())
        if (
            public_root in {PurePosixPath(""), PurePosixPath(".")}
            or public_root.is_absolute()
            or ".." in public_root.parts
            or manifest_relative.parent != public_root
        ):
            raise SystemExit(f"Unsafe public root: {public_root}")
        release_root = repository / Path(*public_root.parts)
        if schema_version == "student-data-release-v1" and any(
            candidate.is_file() and candidate.suffix.lower() == ".ipynb"
            for candidate in release_root.rglob("*")
        ):
            raise SystemExit(f"Data-only release root contains a public notebook: {public_root}")
        if schema_version == "student-data-release-v1":
            notebook = manifest.get("notebook")
            if (
                not isinstance(notebook, dict)
                or notebook.get("public") is not False
                or notebook.get("deliveryChannel") != "private-classroom-drive"
                or "path" in notebook
            ):
                raise SystemExit(f"Data-only release must keep its notebook private: {manifest_path}")
            declared = list(manifest.get("resources", []))
        else:
            declared = [manifest.get("notebook"), *manifest.get("resources", [])]
        for record in declared:
            validate_declared_file(repository, record)
            declared_path = PurePosixPath(str(record.get("path", "")))
            if public_root not in declared_path.parents:
                raise SystemExit(f"Declared file escapes the class release root: {declared_path}")
        tags.append(tag)

    if not tags:
        print("No untagged student data release manifest requires recovery.")
        return 0
    for tag in tags:
        git("tag", tag, args.commit)
    git("push", "--atomic", "origin", *[f"refs/tags/{tag}" for tag in tags])
    remote_tags = remote_tag_commits()
    for tag in tags:
        if remote_tags.get(tag) != args.commit:
            raise SystemExit(f"Remote tag verification failed: {tag}")
        print(f"Created immutable student-data release tag {tag} at {args.commit}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
