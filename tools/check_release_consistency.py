#!/usr/bin/env python3
"""Check that repository release-version references agree."""

from __future__ import annotations

import argparse
import ast
import re
import sys
import tomllib
from pathlib import Path

SEMVER_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


class ReleaseConsistencyError(RuntimeError):
    """A release metadata file is missing or malformed."""


def load_toml(path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ReleaseConsistencyError(f"cannot read TOML {path}: {error}") from error


def source_version(path: Path) -> str:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError) as error:
        raise ReleaseConsistencyError(f"cannot parse {path}: {error}") from error
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in node.targets
        ):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return node.value.value
    raise ReleaseConsistencyError(f"{path} has no literal __version__ assignment")


def lock_version(path: Path) -> str:
    lock = load_toml(path)
    for package in lock.get("package", []):
        if isinstance(package, dict) and package.get("name") == "orchestrator-engine":
            version = package.get("version")
            if isinstance(version, str):
                return version
    raise ReleaseConsistencyError(f"{path} has no orchestrator-engine package")


def require_text(path: Path, needle: str, errors: list[str]) -> None:
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        errors.append(f"cannot read {path}: {error}")
        return
    if needle not in content:
        errors.append(f"{path} is missing expected release marker: {needle}")


def check_release_consistency(root: Path) -> tuple[str, list[str]]:
    root = root.resolve()
    project = load_toml(root / "pyproject.toml")
    version = project.get("project", {}).get("version")
    if not isinstance(version, str) or not SEMVER_PATTERN.fullmatch(version):
        raise ReleaseConsistencyError("pyproject.toml project.version must be x.y.z")

    errors: list[str] = []
    sources = {
        "src/orchestrator_engine/__init__.py": source_version(
            root / "src" / "orchestrator_engine" / "__init__.py"
        ),
        "uv.lock": lock_version(root / "uv.lock"),
    }
    for source, found in sources.items():
        if found != version:
            errors.append(f"{source} has version {found}; expected {version}")

    require_text(root / "CHANGELOG.md", f"## [{version}] -", errors)
    require_text(
        root / "docs" / "setup-guide.md",
        f"OrchestratorEngine.git@v{version}",
        errors,
    )
    require_text(
        root / "docs" / "upgrade-guide.md",
        f"The current release is `{version}`",
        errors,
    )
    require_text(
        root / "docs" / "upgrade-guide.md",
        f"OrchestratorEngine.git@v{version}",
        errors,
    )
    return version, errors


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root (defaults to the script checkout).",
    )
    parser.add_argument(
        "--print-version",
        action="store_true",
        help="Print only the consistent version on success.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        version, errors = check_release_consistency(args.root)
    except ReleaseConsistencyError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    message = (
        version
        if args.print_version
        else f"Release metadata is consistent: {version}"
    )
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
