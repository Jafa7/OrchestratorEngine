#!/usr/bin/env python3
# ruff: noqa: E501
"""Measure compact status reads against naive cumulative-log polling."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock
from xml.sax.saxutils import escape

from orchestrator_engine import binding, core, diagnostics, status, workers

POLL_COUNT = 4
LARGE_LOG_BYTES = 32 * 1024
FIXED_TIME = "2026-01-01T00:00:00.000+00:00"
FIXED_ENGINE_VERSION = "benchmark"


@dataclass(frozen=True)
class Scenario:
    key: str
    title: str
    detail: str
    task_count: int
    bytes_per_task: int


SCENARIOS = (
    Scenario("long-test", "Long test", "1 task | 256 KB log", 1, 256 * 1024),
    Scenario("ai-worker", "AI worker", "1 task | 1 MB log", 1, 1024 * 1024),
    Scenario(
        "parallel-workers",
        "Parallel workers",
        "3 tasks | 512 KB logs",
        3,
        512 * 1024,
    ),
)


def write_fixture_layout(project: Path) -> None:
    core.events_root(project).mkdir(parents=True, exist_ok=True)
    (core.inbox_root(project) / "signals").mkdir(parents=True, exist_ok=True)
    config = workers.workers_config_path(project)
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        """
[workers.fixture]
enabled = true
command = ["python3", "-c", "print('fixture')"]
prompt_via = "stdin"
permission_profile = "full"
""".lstrip(),
        encoding="utf-8",
    )
    binding.write_binding(
        project,
        host="codex",
        target_thread_id="benchmark-thread",
    )


def write_task(
    project: Path,
    task_id: str,
    *,
    status_value: str,
    log_bytes: int,
) -> Path:
    task_dir = workers.task_dir_for(project, task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = task_dir / "worker-stdout.log"
    stdout_path.write_bytes(b"x" * log_bytes)
    (task_dir / "worker-stderr.log").write_bytes(b"")
    (task_dir / "supervisor.log").write_bytes(b"")
    descriptor: dict[str, Any] = {
        "schema_version": core.SCHEMA_VERSION,
        "kind": workers.TASK_KIND,
        "task_id": task_id,
        "worker": "fixture",
        "status": status_value,
        "created_at": FIXED_TIME,
        "last_alive_at": core.utc_now(),
        "supervisor_pid": os.getpid(),
        "worker_pid": os.getpid(),
    }
    if status_value == "completed":
        descriptor["finished_at"] = FIXED_TIME
        core.atomic_json(
            task_dir / "result.json",
            {"terminal_status": "completed", "exit_code": 0},
        )
        core.atomic_json(
            task_dir / "evidence.json",
            {"task_id": task_id, "summary": "fixture completed"},
        )
    core.atomic_json(task_dir / "task.json", descriptor)
    return stdout_path


def normalized_status(report: dict[str, Any], project: Path) -> dict[str, Any]:
    root = str(project)

    def normalize(value: Any, *, key: str | None = None) -> Any:
        if isinstance(value, dict):
            return {name: normalize(item, key=name) for name, item in value.items()}
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if key == "generated_at" and isinstance(value, str):
            return FIXED_TIME
        if key == "engine_version" and isinstance(value, str):
            return FIXED_ENGINE_VERSION
        if isinstance(value, str):
            return value.replace(root, "/benchmark/project")
        return value

    normalized = normalize(report)
    if not isinstance(normalized, dict):
        raise RuntimeError("normalized status must remain an object")
    return normalized


def status_packet_bytes(report: dict[str, Any], project: Path) -> int:
    payload = (
        json.dumps(
            normalized_status(report, project),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    return len(payload.encode("utf-8"))


def installed_engine_check() -> dict[str, Any]:
    return diagnostics.check(
        "engine_import",
        "Engine package is installed for re-exec",
        "ok",
        f"installed package version {FIXED_ENGINE_VERSION}",
        data={"installed_version": FIXED_ENGINE_VERSION},
    )


def run_scenario(parent: Path, scenario: Scenario) -> dict[str, Any]:
    project = parent / scenario.key
    project.mkdir()
    write_fixture_layout(project)
    naive_bytes = 0
    compact_bytes = 0
    final_report: dict[str, Any] | None = None
    cursor: str | None = None

    for poll_index in range(1, POLL_COUNT + 1):
        current_size = scenario.bytes_per_task * poll_index // POLL_COUNT
        terminal = poll_index == POLL_COUNT
        current_status = "completed" if terminal else "running"
        current_logs = []
        for task_index in range(1, scenario.task_count + 1):
            current_logs.append(
                write_task(
                    project,
                    f"TASK-{task_index}",
                    status_value=current_status,
                    log_bytes=current_size,
                )
            )
        naive_bytes += sum(path.stat().st_size for path in current_logs)
        with mock.patch.object(
            diagnostics,
            "check_engine_import",
            return_value=installed_engine_check(),
        ):
            final_report = status.run_status(
                project,
                minimum_severity="warning",
                large_log_bytes=LARGE_LOG_BYTES,
                since_cursor=cursor,
            )
        cursor = str(final_report["cursor"])
        compact_bytes += status_packet_bytes(final_report, project)
        tasks = final_report["components"]["worker_tasks"]
        if tasks["task_count"] != scenario.task_count:
            raise RuntimeError(f"{scenario.key}: status omitted a task")
        if tasks["status_counts"][current_status] != scenario.task_count:
            raise RuntimeError(f"{scenario.key}: status omitted current task state")

    if final_report is None:
        raise RuntimeError(f"{scenario.key}: no status report produced")
    tasks = final_report["components"]["worker_tasks"]
    if tasks["large_log_task_count"] != scenario.task_count:
        raise RuntimeError(f"{scenario.key}: large logs were not addressable")
    for task in tasks["large_log_tasks"].values():
        path = Path(task["large_log_paths"]["stdout"])
        if not path.is_file() or path.stat().st_size != scenario.bytes_per_task:
            raise RuntimeError(f"{scenario.key}: full log path failed quality guard")
        task_dir = path.parent
        if not (task_dir / "result.json").is_file():
            raise RuntimeError(f"{scenario.key}: result artifact is missing")
        if not (task_dir / "evidence.json").is_file():
            raise RuntimeError(f"{scenario.key}: evidence artifact is missing")

    context_share = compact_bytes / naive_bytes
    return {
        "key": scenario.key,
        "title": scenario.title,
        "detail": scenario.detail,
        "poll_count": POLL_COUNT,
        "task_count": scenario.task_count,
        "final_log_bytes": scenario.bytes_per_task * scenario.task_count,
        "naive_polling_bytes": naive_bytes,
        "orchestrator_status_bytes": compact_bytes,
        "context_share_percent": round(context_share * 100, 2),
        "context_reduction_percent": round((1 - context_share) * 100, 2),
        "quality_guard": "passed",
    }


def run_benchmark() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="orchestrator-context-") as temporary:
        parent = Path(temporary)
        scenarios = [run_scenario(parent, scenario) for scenario in SCENARIOS]
    return {
        "schema_version": 1,
        "kind": "ORCHESTRATOR_COORDINATION_CONTEXT_BENCHMARK",
        "metric": "utf8_bytes_presented_during_four_polling_checks",
        "baseline": "read_all_cumulative_worker_stdout_logs_at_each_poll",
        "orchestrated": "read_full_status_once_then_cursor_deltas",
        "large_log_threshold_bytes": LARGE_LOG_BYTES,
        "scenarios": scenarios,
    }


def render_svg(report: dict[str, Any]) -> str:
    scenarios = report["scenarios"]
    groups = []
    centers = (300, 610, 920)
    for center, scenario in zip(centers, scenarios, strict=True):
        share = float(scenario["context_share_percent"])
        green_height = max(4.0, share * 4)
        green_y = 570 - green_height
        groups.append(
            f"""
  <rect x="{center - 110}" y="170" width="105" height="400" rx="5" fill="#64748b"/>
  <rect x="{center + 5}" y="{green_y:.2f}" width="105" height="{green_height:.2f}" rx="3" fill="#16a34a"/>
  <text x="{center - 57.5}" y="156" text-anchor="middle" font-family="Inter, Arial, sans-serif" font-size="15" font-weight="700" fill="#334155">100%</text>
  <text x="{center + 57.5}" y="{green_y - 12:.2f}" text-anchor="middle" font-family="Inter, Arial, sans-serif" font-size="16" font-weight="700" fill="#15803d">{share:.2f}%</text>
  <text x="{center}" y="610" text-anchor="middle" font-family="Inter, Arial, sans-serif" font-size="17" font-weight="700" fill="#0f172a">{escape(str(scenario['title']))}</text>
  <text x="{center}" y="636" text-anchor="middle" font-family="Inter, Arial, sans-serif" font-size="15" font-weight="700" fill="#15803d">{scenario['context_reduction_percent']:.2f}% less context</text>
  <text x="{center}" y="658" text-anchor="middle" font-family="Inter, Arial, sans-serif" font-size="13" fill="#64748b">{escape(str(scenario['detail']))} | 4 checks</text>"""
        )
    group_markup = "\n".join(groups)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="760" viewBox="0 0 1200 760" role="img" aria-labelledby="title description">
  <title id="title">Context read while checking background work</title>
  <desc id="description">Three comparisons show naive cumulative-log polling at 100 percent and compact OrchestratorEngine status reads for a long test, one AI worker, and three parallel workers. Lower is better.</desc>
  <rect width="1200" height="760" fill="#ffffff"/>
  <text x="600" y="54" text-anchor="middle" font-family="Inter, Arial, sans-serif" font-size="30" font-weight="700" fill="#0f172a">Context read while checking background work</text>
  <text x="600" y="84" text-anchor="middle" font-family="Inter, Arial, sans-serif" font-size="16" fill="#475569">Four polling checks as logs grow | measured UTF-8 bytes | lower is better</text>
  <rect x="330" y="108" width="18" height="18" rx="2" fill="#64748b"/>
  <text x="358" y="122" font-family="Inter, Arial, sans-serif" font-size="15" fill="#334155">Read cumulative logs every time</text>
  <rect x="690" y="108" width="18" height="18" rx="2" fill="#16a34a"/>
  <text x="718" y="122" font-family="Inter, Arial, sans-serif" font-size="15" fill="#334155">Read OrchestratorEngine status</text>
  <line x1="110" y1="570" x2="1120" y2="570" stroke="#94a3b8" stroke-width="1.5"/>
  <line x1="110" y1="470" x2="1120" y2="470" stroke="#e2e8f0"/>
  <line x1="110" y1="370" x2="1120" y2="370" stroke="#e2e8f0"/>
  <line x1="110" y1="270" x2="1120" y2="270" stroke="#e2e8f0"/>
  <line x1="110" y1="170" x2="1120" y2="170" stroke="#e2e8f0"/>
  <text x="96" y="575" text-anchor="end" font-family="Inter, Arial, sans-serif" font-size="14" fill="#64748b">0%</text>
  <text x="96" y="475" text-anchor="end" font-family="Inter, Arial, sans-serif" font-size="14" fill="#64748b">25%</text>
  <text x="96" y="375" text-anchor="end" font-family="Inter, Arial, sans-serif" font-size="14" fill="#64748b">50%</text>
  <text x="96" y="275" text-anchor="end" font-family="Inter, Arial, sans-serif" font-size="14" fill="#64748b">75%</text>
  <text x="96" y="175" text-anchor="end" font-family="Inter, Arial, sans-serif" font-size="14" fill="#64748b">100%</text>
{group_markup}
  <rect x="110" y="682" width="1010" height="58" rx="8" fill="#f0fdf4" stroke="#bbf7d0"/>
  <text x="615" y="704" text-anchor="middle" font-family="Inter, Arial, sans-serif" font-size="13" font-weight="700" fill="#166534">Every task state remains visible | full result, evidence and logs stay available by path</text>
  <text x="615" y="724" text-anchor="middle" font-family="Inter, Arial, sans-serif" font-size="12" fill="#166534">Selective inspection, not output truncation; open complete artifacts whenever a status requires drill-down</text>
  <text x="615" y="739" text-anchor="middle" font-family="Inter, Arial, sans-serif" font-size="11" fill="#475569">Synthetic deterministic fixture | context-volume proxy, not a promise about total tokens, latency or engineering productivity</text>
</svg>
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-svg", type=Path)
    args = parser.parse_args()
    report = run_benchmark()
    if args.write_svg is not None:
        args.write_svg.parent.mkdir(parents=True, exist_ok=True)
        args.write_svg.write_text(render_svg(report), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
