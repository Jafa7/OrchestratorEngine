#!/usr/bin/env python3
"""Run bounded installed-wheel acceptance on a native Windows or macOS host."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    from tools import run_reliability_soak
except ModuleNotFoundError:  # Direct `python tools/...` execution.
    import run_reliability_soak  # type: ignore[no-redef]

KIND = "ORCHESTRATOR_NATIVE_ACCEPTANCE_REPORT"
SUPPORTED_SYSTEMS = {"Darwin": "darwin", "Windows": "windows"}


class NativeAcceptanceError(RuntimeError):
    """The host or installed CLI cannot satisfy native acceptance."""


def _digest_text(value: str) -> dict[str, Any]:
    data = value.encode("utf-8", errors="replace")
    return {"size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def _machine_family(value: str) -> str:
    lowered = value.strip().lower()
    if lowered in {"amd64", "x86_64"}:
        return "x86_64"
    if lowered in {"aarch64", "arm64"}:
        return "arm64"
    return lowered


def _host_metadata(system: str) -> dict[str, str]:
    machine = platform.machine()
    return {
        "system": system,
        "release": platform.release(),
        "machine": machine,
        "machine_family": _machine_family(machine),
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
    }


def _field_verification_boundary() -> dict[str, dict[str, str]]:
    return {
        name: {
            "status": "not_tested",
            "reason": "requires adopter environment testing",
        }
        for name in (
            "desktop_live_delivery",
            "login_restart",
            "sleep_resume",
            "os_reboot",
            "desktop_update",
            "nonlocal_filesystem",
        )
    }


def _privacy_contract() -> dict[str, bool]:
    return {
        "hostname_omitted": True,
        "local_paths_omitted": True,
        "raw_command_output_omitted": True,
    }


def _sanitize_soak_report(report: dict[str, Any]) -> dict[str, Any]:
    sanitized = {
        key: report.get(key)
        for key in (
            "schema_version",
            "kind",
            "status",
            "mode",
            "iterations_requested",
            "iterations_completed",
            "duration_seconds",
            "conformance_duration_seconds",
        )
    }
    failure = report.get("failure")
    if isinstance(failure, dict):
        sanitized_failure = {
            key: failure.get(key)
            for key in ("iteration", "exit_code", "type")
        }
        message = failure.get("message")
        if message is not None:
            sanitized_failure["message"] = _digest_text(str(message))
        fixture = failure.get("fixture")
        sanitized_failure["fixture_retained"] = fixture is not None
        if fixture is not None:
            sanitized_failure["fixture_pointer"] = _digest_text(str(fixture))
        sanitized["failure"] = sanitized_failure
    return sanitized


def _run_json(cli: Path, *arguments: str, timeout_seconds: float) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            [str(cli), *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        raise NativeAcceptanceError("installed CLI diagnostic timed out") from error
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise NativeAcceptanceError(
            "installed CLI diagnostic returned invalid JSON"
        ) from error
    if completed.returncode != 0 or not isinstance(value, dict):
        raise NativeAcceptanceError("installed CLI diagnostic failed")
    return {
        "exit_code": completed.returncode,
        "stdout": _digest_text(completed.stdout),
        "stderr": _digest_text(completed.stderr),
        "value": value,
    }


def _run_version(cli: Path, *, timeout_seconds: float) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            [str(cli), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        raise NativeAcceptanceError("installed CLI version check timed out") from error
    version = completed.stdout.strip()
    if completed.returncode != 0 or not version or len(version) > 200:
        raise NativeAcceptanceError("installed CLI version check failed")
    return {
        "exit_code": completed.returncode,
        "version": version,
        "stdout": _digest_text(completed.stdout),
        "stderr": _digest_text(completed.stderr),
    }


def run_acceptance(
    cli: Path,
    *,
    iterations: int,
    timeout_seconds: float,
    system: str | None = None,
    expected_system: str | None = None,
    expected_machine: str | None = None,
) -> dict[str, Any]:
    observed_system = system or platform.system()
    expected_platform = SUPPORTED_SYSTEMS.get(observed_system)
    if expected_platform is None:
        raise NativeAcceptanceError(
            "native acceptance requires Windows or Darwin"
        )
    if expected_system is not None and observed_system != expected_system:
        raise NativeAcceptanceError(
            f"expected system {expected_system}, found {observed_system}"
        )
    if iterations < 1 or iterations > run_reliability_soak.MAX_ITERATIONS:
        raise NativeAcceptanceError(
            "iterations must be between "
            f"1 and {run_reliability_soak.MAX_ITERATIONS}"
        )
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise NativeAcceptanceError("timeout must be finite and positive")
    observed_machine = platform.machine()
    if (
        expected_machine is not None
        and _machine_family(expected_machine) not in {"arm64", "x86_64"}
    ):
        raise NativeAcceptanceError(
            "expected machine must identify arm64 or x86_64"
        )
    if (
        expected_machine is not None
        and _machine_family(observed_machine) != _machine_family(expected_machine)
    ):
        raise NativeAcceptanceError(
            f"expected machine family {_machine_family(expected_machine)}, "
            f"found {_machine_family(observed_machine)}"
        )

    version = _run_version(cli, timeout_seconds=timeout_seconds)
    capabilities = _run_json(
        cli, "runtime-capabilities", timeout_seconds=timeout_seconds
    )
    value = capabilities.pop("value")
    capability_valid = (
        value.get("kind") == "ORCHESTRATOR_PLATFORM_CAPABILITIES"
        and value.get("platform") == expected_platform
        and value.get("portable_core") == "supported"
        and value.get("file_locking") == "supported"
        and value.get("detached_lifecycle") == "supported"
    )
    raw_soak = run_reliability_soak.run_soak(
        cli,
        iterations=iterations,
        mode="full",
        timeout_seconds=timeout_seconds,
    )
    soak = _sanitize_soak_report(raw_soak)
    passed = capability_valid and soak.get("status") == "passed"
    return {
        "schema_version": 1,
        "kind": KIND,
        "status": "passed" if passed else "failed",
        "host": _host_metadata(observed_system),
        "installed_cli": version,
        "runtime_capabilities": {
            "status": "passed" if capability_valid else "failed",
            "kind": value.get("kind"),
            "platform": value.get("platform"),
            "portable_core": value.get("portable_core"),
            "file_locking": value.get("file_locking"),
            "detached_lifecycle": value.get("detached_lifecycle"),
            **capabilities,
        },
        "reliability_soak": soak,
        "field_verification": _field_verification_boundary(),
        "privacy": _privacy_contract(),
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", type=Path, default=Path("orchestrator-engine"))
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--timeout-seconds", type=float, default=15)
    parser.add_argument("--expected-system", choices=tuple(SUPPORTED_SYSTEMS))
    parser.add_argument("--expected-machine")
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        report = run_acceptance(
            args.cli.expanduser(),
            iterations=args.iterations,
            timeout_seconds=args.timeout_seconds,
            expected_system=args.expected_system,
            expected_machine=args.expected_machine,
        )
    except (
        OSError,
        NativeAcceptanceError,
        run_reliability_soak.ReliabilitySoakError,
    ) as error:
        failure: dict[str, Any] = {"type": type(error).__name__}
        if isinstance(error, NativeAcceptanceError):
            failure["message"] = str(error)[:500]
        else:
            failure["message"] = _digest_text(str(error))
        report = {
            "schema_version": 1,
            "kind": KIND,
            "status": "failed",
            "host": _host_metadata(platform.system()),
            "failure": failure,
            "field_verification": _field_verification_boundary(),
            "privacy": _privacy_contract(),
        }
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
