"""First-class check projection for resource-managed verification recipes."""

from __future__ import annotations

import math

from . import core, local_checks, verification
from .resource_queue import ResourceError
from .resource_service import client, input_manifest


def start(
    project,
    *,
    check_id,
    spec,
    execution,
    wake_policy,
    state_dir,
    long_threshold_seconds,
):
    if not math.isfinite(long_threshold_seconds) or long_threshold_seconds <= 0:
        raise ResourceError("long threshold must be positive and finite")
    if state_dir != core.DEFAULT_STATE_DIR or execution == "foreground":
        raise ResourceError(
            "resource checks use the default state directory and detached execution"
        )
    requested_wake_policy = wake_policy
    wake_policy = local_checks.resolved_wake_policy(wake_policy, "detached")
    target = local_checks.capture_wake_target(
        project, state_dir=state_dir, wake_policy=wake_policy
    )
    verification.claim_check_owner(
        project,
        operation_id=check_id,
        operation_type="local_check",
        state_dir=state_dir,
    )
    path = local_checks.descriptor_path(project, check_id, state_dir=state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = core.load_object(project / state_dir / "resources.json")
    with local_checks.file_lock(path.with_suffix(".lock")):
        if path.exists():
            descriptor = local_checks.load_check_descriptor(path)
            if (
                descriptor.get("fingerprint") != spec["fingerprint"]
                or descriptor.get("wake_policy") != wake_policy
                or not descriptor.get("resource_managed")
            ):
                raise ResourceError("check ID already has different options")
            if descriptor.get("resource_request"):
                return {
                    k: v for k, v in descriptor.items() if k != "resource_submission"
                }
        else:
            contract = client(connection, "recipe", {"recipe": spec["resource_recipe"]})
            subscriber = {
                "id": check_id,
                "check_id": check_id,
                "wake": wake_policy == "always",
                "wake_policy": wake_policy,
                "wake_target": target,
            }
            descriptor = {
                "schema_version": core.SCHEMA_VERSION,
                "kind": local_checks.CHECK_KIND,
                "check_id": check_id,
                "suite": spec["suite"],
                "fingerprint": spec["fingerprint"],
                "verification": spec["verification"],
                "status": "starting",
                "execution": "detached",
                "requested_execution": execution,
                "wake_policy": wake_policy,
                "requested_wake_policy": requested_wake_policy,
                "long_threshold_seconds": long_threshold_seconds,
                "plan": {
                    "resource_recipe": spec["resource_recipe"],
                    "recommended_execution": "detached",
                },
                "check_dir": str(path.parent),
                "resource_managed": True,
                "created_at": core.utc_now(),
                "resource_submission": {
                    "id": "check:" + check_id,
                    "recipe": spec["resource_recipe"],
                    "recipe_digest": contract["recipe_digest"],
                    "inputs": input_manifest(project, contract["inputs"]),
                    "subscriber": subscriber,
                },
            }
            core.atomic_json(path, descriptor)
        result = client(connection, "submit", descriptor["resource_submission"])
        descriptor["resource_request"] = result["request"]
        descriptor["resource_contract_digest"] = result["contract_digest"]
        core.atomic_json(path, descriptor)
    return {k: v for k, v in descriptor.items() if k != "resource_submission"}


def status(project, descriptor):
    result = {
        "check_id": descriptor["check_id"],
        "suite": descriptor["suite"],
        "execution": "detached",
        "wake_policy": descriptor["wake_policy"],
        "resource_request": descriptor.get("resource_request"),
        "status": descriptor["status"],
    }
    if not descriptor.get("resource_request"):
        return {
            **result,
            "status": "stalled",
            "failure_kind": "submission_response_unknown",
        }
    if descriptor["status"] in local_checks.TERMINAL_STATUSES:
        return {
            **result,
            **{
                key: descriptor[key]
                for key in (
                    "result_path",
                    "evidence_path",
                    "finished_at",
                    "duration_seconds",
                )
                if key in descriptor
            },
        }
    try:
        connection = core.load_object(
            project / core.DEFAULT_STATE_DIR / "resources.json"
        )
        snapshot = client(
            connection, "status", {"request": descriptor["resource_request"]}
        )
    except ResourceError:
        return {
            **result,
            "status": "stalled",
            "failure_kind": "resource_authority_unavailable",
        }
    if snapshot["action_required"]:
        result.update(
            status="stalled", failure_kind="resource_recovery_or_delivery_required"
        )
    elif snapshot["terminal"]:
        # Check readiness includes the durable result projection.
        result["status"] = "starting"
        result["failure_kind"] = "resource_result_delivery_pending"
    elif any(s["state"] == "running" for s in snapshot["stages"]):
        result["status"] = "running"
    else:
        result["status"] = "starting"
    result["resource_stages"] = [
        {
            key: stage[key]
            for key in ("name", "state", "reason", "allocation", "epoch")
            if key in stage
        }
        for stage in snapshot["stages"]
    ]
    return result


def project_result(project, subscriber, value, request, stages):
    check_id = local_checks.validate_id(subscriber["check_id"], field="check id")
    path = local_checks.descriptor_path(
        project, check_id, state_dir=core.DEFAULT_STATE_DIR
    )
    with local_checks.file_lock(path.with_suffix(".lock")):
        descriptor = local_checks.load_check_descriptor(path)
        if not descriptor.get("resource_managed"):
            raise ResourceError("terminal projection does not own this check")
        if descriptor.get("resource_request") not in {None, request["id"]}:
            raise ResourceError("terminal projection belongs to another request")
        if descriptor["resource_submission"]["id"] != request["external"]:
            raise ResourceError("check projection has a different submission identity")
        if value["status"] == "action_required":
            # Quiescence is unresolved: do not manufacture a terminal test failure.
            result_path = path.parent / f"recovery-required-{value['outcome_id']}.json"
            if not result_path.exists():
                core.atomic_json(
                    result_path,
                    {
                        "kind": "ORCHESTRATOR_RESOURCE_RECOVERY_REQUIRED",
                        "schema_version": core.SCHEMA_VERSION,
                        "check_id": check_id,
                        "resource_request": request["id"],
                        "status": "action_required",
                        "stages": stages,
                    },
                )
            return result_path
        result_path = path.parent / "verification-result.json"
        if not result_path.exists():
            status = "passed" if value["status"] == "passed" else "failed"
            commands = []
            for stage in stages:
                for item in stage.get("results", []):
                    commands.append(
                        {
                            **item,
                            "label": f"{stage['name']}:{item.get('index')}",
                            "required": True,
                            "status": "passed"
                            if item.get("exit_code") == 0
                            and item.get("reason") == "completed"
                            else "failed",
                        }
                    )
            core.atomic_json(
                result_path,
                {
                    "schema_version": core.SCHEMA_VERSION,
                    "kind": verification.VERIFICATION_RESULT_KIND,
                    "check_id": check_id,
                    "suite": descriptor["suite"],
                    "status": status,
                    "exit_code": 0 if status == "passed" else 1,
                    "execution": "detached",
                    "fingerprint": descriptor["fingerprint"],
                    "commands": commands,
                    "resource_request": request["id"],
                    "input_manifest": request["plan"]["input_manifest"],
                    "stages": stages,
                    "finished_at": core.utc_now(),
                },
            )
        descriptor.update(
            status="passed" if value["status"] == "passed" else "failed",
            resource_request=request["id"],
            result_path=str(result_path),
            evidence_path=str(result_path),
            finished_at=core.utc_now(),
        )
        core.atomic_json(path, descriptor)
    return result_path
