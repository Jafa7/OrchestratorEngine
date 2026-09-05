#!/usr/bin/env python3
"""Prepare deterministic release notes and checksums for built distributions."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tomllib
from pathlib import Path

SEMVER_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


class ReleaseBundleError(RuntimeError):
    """Release source metadata or built assets are inconsistent."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def project_version(root: Path) -> str:
    try:
        with (root / "pyproject.toml").open("rb") as handle:
            version = tomllib.load(handle).get("project", {}).get("version")
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ReleaseBundleError(f"cannot read pyproject.toml: {error}") from error
    if not isinstance(version, str) or not SEMVER_PATTERN.fullmatch(version):
        raise ReleaseBundleError("project version must use x.y.z form")
    return version


def changelog_section(root: Path, version: str) -> str:
    path = root / "CHANGELOG.md"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise ReleaseBundleError(f"cannot read CHANGELOG.md: {error}") from error
    marker = re.compile(rf"^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}$")
    start = next(
        (
            index + 1
            for index, line in enumerate(lines)
            if marker.fullmatch(line)
        ),
        None,
    )
    if start is None:
        raise ReleaseBundleError(f"CHANGELOG.md has no release section for {version}")
    end = next(
        (
            index
            for index in range(start, len(lines))
            if lines[index].startswith("## [")
        ),
        len(lines),
    )
    section = "\n".join(lines[start:end]).strip()
    if not section:
        raise ReleaseBundleError(f"CHANGELOG.md release section for {version} is empty")
    return section


def distribution_paths(dist_dir: Path, version: str) -> list[Path]:
    expected = {
        f"orchestrator_engine-{version}-py3-none-any.whl",
        f"orchestrator_engine-{version}.tar.gz",
    }
    actual = {
        path.name
        for path in dist_dir.iterdir()
        if path.is_file() and (path.suffix == ".whl" or path.name.endswith(".tar.gz"))
    }
    if actual != expected:
        raise ReleaseBundleError(
            "distribution set does not match the release version: "
            f"expected={sorted(expected)!r} actual={sorted(actual)!r}"
        )
    return [dist_dir / name for name in sorted(expected)]


def expected_assets(
    paths: list[Path], checksums_path: Path
) -> dict[str, dict[str, object]]:
    all_paths = [*paths, checksums_path]
    return {
        path.name: {"size": path.stat().st_size, "digest": f"sha256:{sha256(path)}"}
        for path in all_paths
    }


def verify_remote_assets(path: Path, expected: dict[str, dict[str, object]]) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseBundleError(
            f"cannot read remote release assets: {error}"
        ) from error
    assets = payload.get("assets") if isinstance(payload, dict) else None
    if not isinstance(assets, list) or any(
        not isinstance(item, dict) for item in assets
    ):
        raise ReleaseBundleError("remote release assets must be an array of objects")
    remote = {
        item.get("name"): item
        for item in assets
        if isinstance(item.get("name"), str)
    }
    if len(remote) != len(assets):
        raise ReleaseBundleError("remote release assets contain invalid names")
    if set(remote) != set(expected):
        raise ReleaseBundleError(
            "remote release asset names differ from the prepared bundle"
        )
    for name, local in expected.items():
        item = remote[name]
        if item.get("state") != "uploaded":
            raise ReleaseBundleError(f"remote release asset is not uploaded: {name}")
        if item.get("size") != local["size"] or item.get("digest") != local["digest"]:
            raise ReleaseBundleError(f"remote release asset digest mismatch: {name}")


def prepare_bundle(
    *,
    root: Path,
    tag: str,
    dist_dir: Path,
    output_dir: Path,
    verify_assets_path: Path | None = None,
) -> dict[str, object]:
    version = project_version(root)
    if tag != f"v{version}":
        raise ReleaseBundleError(
            f"tag {tag!r} does not match project version {version!r}"
        )
    if not dist_dir.is_dir():
        raise ReleaseBundleError(f"distribution directory does not exist: {dist_dir}")
    paths = distribution_paths(dist_dir, version)
    output_dir.mkdir(parents=True, exist_ok=True)
    notes_path = output_dir / "release-notes.md"
    notes_path.write_text(
        changelog_section(root, version)
        + "\n\n## Installation\n\n"
        + "Install from the immutable tag or the attached wheel. See the "
        + f"[upgrade guide](https://github.com/Jafa7/OrchestratorEngine/blob/{tag}/docs/upgrade-guide.md).\n",
        encoding="utf-8",
        newline="\n",
    )
    checksums_path = output_dir / "SHA256SUMS"
    checksums_path.write_text(
        "".join(f"{sha256(path)}  {path.name}\n" for path in paths),
        encoding="utf-8",
        newline="\n",
    )
    expected = expected_assets(paths, checksums_path)
    if verify_assets_path is not None:
        verify_remote_assets(verify_assets_path, expected)
    return {
        "schema_version": 1,
        "kind": "ORCHESTRATOR_RELEASE_BUNDLE",
        "version": version,
        "tag": tag,
        "notes_path": str(notes_path),
        "checksums_path": str(checksums_path),
        "assets": [
            {"name": name, **metadata}
            for name, metadata in sorted(expected.items())
        ],
        "remote_assets_verified": verify_assets_path is not None,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--tag", required=True)
    parser.add_argument("--dist-dir", type=Path, default=Path("dist"))
    parser.add_argument("--output-dir", type=Path, default=Path("release"))
    parser.add_argument("--verify-assets", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        report = prepare_bundle(
            root=args.root.resolve(),
            tag=args.tag,
            dist_dir=args.dist_dir.resolve(),
            output_dir=args.output_dir.resolve(),
            verify_assets_path=(
                args.verify_assets.resolve() if args.verify_assets is not None else None
            ),
        )
    except (OSError, ReleaseBundleError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
