"""Offline validation for versioned synthetic benchmark run records.

This module performs no model inference, network access, evidence selection, or
filesystem access. It checks whether two already-described synthetic run
records are comparable; it does not score model output or establish outcomes.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,96}\Z")
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_COMMIT = re.compile(r"[a-f0-9]{40,64}\Z")
_VERSION_PLACEHOLDERS = {
    "default",
    "latest",
    "na",
    "n/a",
    "none",
    "unknown",
    "unavailable",
}
_RUN_FIELDS = {
    "schema_version",
    "cohort_id",
    "fixture_pack_sha256",
    "rubric_sha256",
    "protocol_sha256",
    "fixture_id",
    "pair_id",
    "replicate_id",
    "comparison_axis",
    "arm",
    "order_assignment",
    "order_position",
    "cache_condition",
    "random_seed",
    "memory_snapshot",
    "reset_protocol_id",
    "washout_seconds",
    "workspace_template_sha256",
    "workspace_id",
    "harness_version",
    "harness_commit",
    "model_id",
    "model_version",
    "run_state",
    "terminal_reason",
    "attempts",
}
_RUN_STATES = {"completed", "unresolved", "censored", "aborted"}
_ATTEMPT_STATES = _RUN_STATES | {"failed"}
_TERMINAL_REASONS = {
    "unresolved": {"invalid_output", "missing_source", "other", "unknown_output"},
    "censored": {"budget_exhausted", "deadline", "followup_pending"},
    "aborted": {"environment_failure", "operator_cancel", "policy_block", "other"},
}
_PAIR_MATCH_FIELDS = (
    "cohort_id",
    "fixture_pack_sha256",
    "rubric_sha256",
    "protocol_sha256",
    "fixture_id",
    "pair_id",
    "replicate_id",
    "comparison_axis",
    "random_seed",
    "memory_snapshot",
    "reset_protocol_id",
    "washout_seconds",
    "cache_condition",
    "workspace_template_sha256",
    "order_assignment",
)


def _identifier(value: Any, field: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field} must be an opaque identifier.")


def _digest(value: Any, field: str) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest.")


def _choice(value: Any, choices: set[str], field: str) -> None:
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"{field} has an unsupported value.")


def _validate_memory_snapshot(value: Any) -> None:
    if not isinstance(value, dict):
        raise TypeError("memory_snapshot must be an explicit object.")
    if value.get("state") == "not_applicable":
        if set(value) != {"state", "reason"}:
            raise ValueError(
                "not-applicable memory_snapshot requires only state and reason."
            )
        _identifier(value["reason"], "memory_snapshot.reason")
    elif value.get("state") == "frozen":
        if set(value) != {"state", "sha256"}:
            raise ValueError("frozen memory_snapshot requires only state and sha256.")
        _digest(value["sha256"], "memory_snapshot.sha256")
    else:
        raise ValueError("memory_snapshot state must be frozen or not_applicable.")


def _validate_attempts(value: Any, run_state: str, max_attempts: int) -> int:
    if not isinstance(value, list) or not value or len(value) > max_attempts:
        raise ValueError(
            "attempts must be a non-empty list within the declared budget."
        )

    previous_id: str | None = None
    previous_state: str | None = None
    seen: set[str] = set()
    for index, attempt in enumerate(value, start=1):
        if not isinstance(attempt, dict) or set(attempt) != {
            "attempt_id",
            "attempt_number",
            "outcome",
            "retry_of",
        }:
            raise ValueError(
                "Each attempt must have the exact versioned attempt fields."
            )
        _identifier(attempt["attempt_id"], "attempt_id")
        if attempt["attempt_id"] in seen:
            raise ValueError("attempt_id values must be unique within a run.")
        seen.add(attempt["attempt_id"])
        if (
            type(attempt["attempt_number"]) is not int
            or attempt["attempt_number"] != index
        ):
            raise ValueError(
                "attempt_number values must be contiguous and ordered from one."
            )
        if (
            not isinstance(attempt["outcome"], str)
            or attempt["outcome"] not in _ATTEMPT_STATES
        ):
            raise ValueError("attempt outcome is not a supported run state.")
        if index == 1:
            if attempt["retry_of"] is not None:
                raise ValueError("The first attempt cannot be marked as a retry.")
        elif attempt["retry_of"] != previous_id or previous_state not in {
            "failed",
            "unresolved",
        }:
            raise ValueError("Each retry must point to the preceding failed attempt.")
        if index < len(value) and attempt["outcome"] not in {"failed", "unresolved"}:
            raise ValueError("Only failed or unresolved attempts may be retried.")
        previous_id = attempt["attempt_id"]
        previous_state = attempt["outcome"]

    final_state = value[-1]["outcome"]
    compatible = final_state == run_state or (
        run_state == "unresolved" and final_state == "failed"
    )
    if not compatible:
        raise ValueError("run_state must preserve the final attempt outcome.")
    return len(value)


def _validate_run_record(record: Any, max_attempts: int) -> int:
    if not isinstance(record, dict) or set(record) != _RUN_FIELDS:
        raise ValueError("Run record does not match the exact benchmark schema.")
    if record["schema_version"] != "aiobs.benchmark-run.v1":
        raise ValueError("Unsupported benchmark run schema_version.")

    for field in (
        "cohort_id",
        "fixture_id",
        "pair_id",
        "replicate_id",
        "random_seed",
        "reset_protocol_id",
        "workspace_id",
    ):
        _identifier(record[field], field)
    for field in (
        "fixture_pack_sha256",
        "rubric_sha256",
        "protocol_sha256",
        "workspace_template_sha256",
    ):
        _digest(record[field], field)

    for field in ("harness_version", "model_id", "model_version"):
        if (
            not isinstance(record[field], str)
            or not record[field].strip()
            or record[field] != record[field].strip()
            or len(record[field]) > 256
            or record[field].strip().lower() in _VERSION_PLACEHOLDERS
        ):
            raise ValueError(
                f"{field} must record the actual non-empty version identity."
            )
    if (
        not isinstance(record["harness_commit"], str)
        or not _COMMIT.fullmatch(record["harness_commit"])
        or set(record["harness_commit"]) == {"0"}
    ):
        raise ValueError("harness_commit must be an actual lowercase commit digest.")

    _choice(record["comparison_axis"], {"harness", "model"}, "comparison_axis")
    _choice(record["arm"], {"baseline", "candidate"}, "arm")
    _choice(
        record["order_assignment"],
        {"baseline_first", "candidate_first"},
        "order_assignment",
    )
    if type(record["order_position"]) is not int or record["order_position"] not in {
        1,
        2,
    }:
        raise ValueError("order_position must be the integer 1 or 2.")
    expected_position = {
        "baseline_first": {"baseline": 1, "candidate": 2},
        "candidate_first": {"candidate": 1, "baseline": 2},
    }[record["order_assignment"]][record["arm"]]
    if record["order_position"] != expected_position:
        raise ValueError(
            "order_position conflicts with the assigned counterbalanced order."
        )
    _choice(record["cache_condition"], {"cold", "warm"}, "cache_condition")
    if type(record["washout_seconds"]) is not int or record["washout_seconds"] < 0:
        raise ValueError("washout_seconds must be a non-negative integer.")

    _validate_memory_snapshot(record["memory_snapshot"])
    _choice(record["run_state"], _RUN_STATES, "run_state")
    reason = record["terminal_reason"]
    if record["run_state"] == "completed":
        if reason is not None:
            raise ValueError("completed runs cannot have a terminal_reason.")
    elif (
        not isinstance(reason, str)
        or reason not in _TERMINAL_REASONS[record["run_state"]]
    ):
        raise ValueError(
            "incomplete runs require a safe reason code matching their state."
        )
    return _validate_attempts(record["attempts"], record["run_state"], max_attempts)


def assess_pair(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    artifact_digests: dict[str, str],
    fixture_ids: set[str],
    max_attempts: int,
) -> dict[str, Any]:
    """Classify runs against already-verified artifact identities, offline."""
    if type(max_attempts) is not int or max_attempts < 1:
        raise ValueError("max_attempts must be a positive integer.")
    required_digests = {
        "fixture_pack_sha256",
        "rubric_sha256",
        "protocol_sha256",
    }
    if (
        not isinstance(artifact_digests, dict)
        or set(artifact_digests) != required_digests
    ):
        raise ValueError("artifact_digests must pin the fixture, rubric and protocol.")
    for field, digest in artifact_digests.items():
        _digest(digest, field)
    if not isinstance(fixture_ids, set) or not fixture_ids:
        raise ValueError(
            "fixture_ids must be the non-empty set from the pinned fixture pack."
        )
    for fixture_id in fixture_ids:
        _identifier(fixture_id, "fixture_id")

    baseline_attempts = _validate_run_record(baseline, max_attempts)
    candidate_attempts = _validate_run_record(candidate, max_attempts)
    if baseline["arm"] != "baseline" or candidate["arm"] != "candidate":
        raise ValueError("assess_pair requires baseline then candidate arm records.")

    reasons: list[str] = []
    for field, digest in artifact_digests.items():
        if baseline[field] != digest or candidate[field] != digest:
            reasons.append(f"artifact_not_verified:{field}")
    if (
        baseline["fixture_id"] not in fixture_ids
        or candidate["fixture_id"] not in fixture_ids
    ):
        reasons.append("fixture_not_in_pack")
    for field in _PAIR_MATCH_FIELDS:
        if baseline[field] != candidate[field]:
            reasons.append(f"mismatch:{field}")
    if baseline["workspace_id"] == candidate["workspace_id"]:
        reasons.append("workspace_reused")

    axis = baseline["comparison_axis"]
    if axis == "harness":
        if (baseline["model_id"], baseline["model_version"]) != (
            candidate["model_id"],
            candidate["model_version"],
        ):
            reasons.append("non_comparison_model_changed")
        if (baseline["harness_version"], baseline["harness_commit"]) == (
            candidate["harness_version"],
            candidate["harness_commit"],
        ):
            reasons.append("comparison_harness_unchanged")
    else:
        if (baseline["harness_version"], baseline["harness_commit"]) != (
            candidate["harness_version"],
            candidate["harness_commit"],
        ):
            reasons.append("non_comparison_harness_changed")
        if (baseline["model_id"], baseline["model_version"]) == (
            candidate["model_id"],
            candidate["model_version"],
        ):
            reasons.append("comparison_model_unchanged")

    arm_states = {
        "baseline": baseline["run_state"],
        "candidate": candidate["run_state"],
    }
    if reasons:
        status = "ineligible"
    elif any(state != "completed" for state in arm_states.values()):
        status = "incomplete"
    else:
        status = "eligible"

    return {
        "status": status,
        "reason_codes": reasons,
        "cohort_id": baseline["cohort_id"],
        "artifact_digests": {
            field: baseline[field]
            for field in (
                "fixture_pack_sha256",
                "rubric_sha256",
                "protocol_sha256",
            )
        },
        "fixture_id": baseline["fixture_id"],
        "pair_id": baseline["pair_id"],
        "replicate_id": baseline["replicate_id"],
        "comparison_axis": axis,
        "order_assignment": baseline["order_assignment"],
        "cache_condition": baseline["cache_condition"],
        "versions": {
            arm: {
                "harness_version": record["harness_version"],
                "harness_commit": record["harness_commit"],
                "model_id": record["model_id"],
                "model_version": record["model_version"],
            }
            for arm, record in (("baseline", baseline), ("candidate", candidate))
        },
        "arm_states": arm_states,
        "arm_terminal_reasons": {
            "baseline": baseline["terminal_reason"],
            "candidate": candidate["terminal_reason"],
        },
        "attempt_counts": {
            "baseline": baseline_attempts,
            "candidate": candidate_attempts,
        },
        "attempts_are_independent_samples": False,
        "cluster_unit": "fixture_id",
    }


def summarize_pair_eligibility(
    assessments: list[dict[str, Any]], *, max_replicates_per_fixture: int
) -> dict[str, Any]:
    """Summarize supplied pair manifests only; never imply cohort completeness."""
    if not isinstance(assessments, list):
        raise TypeError("assessments must be a list of pair classifications.")
    if type(max_replicates_per_fixture) is not int or max_replicates_per_fixture < 1:
        raise ValueError("max_replicates_per_fixture must be a positive integer.")

    seen_pairs: set[str] = set()
    seen_replicates: set[tuple[str, str]] = set()
    seen_fixture_cells: set[tuple[str, str, str]] = set()
    fixture_pair_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    order_counts: Counter[str] = Counter()
    cache_counts: Counter[str] = Counter()
    cache_status_counts: dict[str, Counter[str]] = {
        "cold": Counter(),
        "warm": Counter(),
    }
    condition_cells = (
        "cold/baseline_first",
        "cold/candidate_first",
        "warm/baseline_first",
        "warm/candidate_first",
    )
    condition_cell_counts: Counter[str] = Counter()
    condition_cell_status_counts: dict[str, Counter[str]] = {
        cell: Counter() for cell in condition_cells
    }
    reason_code_counts: Counter[str] = Counter()
    arm_state_counts: dict[str, Counter[str]] = {
        "baseline": Counter(),
        "candidate": Counter(),
    }
    arm_terminal_reason_counts: dict[str, Counter[str]] = {
        "baseline": Counter(),
        "candidate": Counter(),
    }
    eligible_fixtures: set[str] = set()
    attempts_seen = 0
    summary_identity: tuple[Any, ...] | None = None
    version_pairs: list[dict[str, Any]] = []
    for item in assessments:
        if not isinstance(item, dict):
            raise TypeError("each assessment must be a pair classification object.")
        if not isinstance(item.get("status"), str) or item["status"] not in {
            "eligible",
            "ineligible",
            "incomplete",
        }:
            raise ValueError("assessment contains an unsupported pair status.")
        pair_id = item.get("pair_id")
        fixture_id = item.get("fixture_id")
        replicate_id = item.get("replicate_id")
        _identifier(pair_id, "pair_id")
        _identifier(fixture_id, "fixture_id")
        _identifier(replicate_id, "replicate_id")
        if pair_id in seen_pairs:
            raise ValueError("pair_id values must be unique in a supplied summary.")
        seen_pairs.add(pair_id)
        fixture_replicate = (fixture_id, replicate_id)
        if fixture_replicate in seen_replicates:
            raise ValueError("replicate_id values must be unique within each fixture.")
        seen_replicates.add(fixture_replicate)
        fixture_pair_counts[fixture_id] += 1
        if fixture_pair_counts[fixture_id] > max_replicates_per_fixture:
            raise ValueError("fixture replicate records exceed the declared budget.")

        states = item.get("arm_states")
        if not isinstance(states, dict) or set(states) != {"baseline", "candidate"}:
            raise ValueError("assessment must preserve both arm states.")
        if any(
            not isinstance(state, str) or state not in _RUN_STATES
            for state in states.values()
        ):
            raise ValueError("assessment contains an unsupported arm state.")
        terminal_reasons = item.get("arm_terminal_reasons")
        if not isinstance(terminal_reasons, dict) or set(terminal_reasons) != {
            "baseline",
            "candidate",
        }:
            raise ValueError("assessment must preserve terminal reason codes by arm.")
        for arm, state in states.items():
            reason = terminal_reasons[arm]
            if state == "completed":
                if reason is not None:
                    raise ValueError(
                        "completed arms cannot have terminal reason codes."
                    )
            elif not isinstance(reason, str) or reason not in _TERMINAL_REASONS[state]:
                raise ValueError(
                    "incomplete arms require their state-specific reason code."
                )
        if item["status"] == "eligible" and any(
            state != "completed" for state in states.values()
        ):
            raise ValueError("an eligible pair must have two completed arms.")
        if item["status"] == "incomplete" and all(
            state == "completed" for state in states.values()
        ):
            raise ValueError("an incomplete pair must preserve a non-completed arm.")
        reason_codes = item.get("reason_codes")
        if (
            not isinstance(reason_codes, list)
            or any(not isinstance(reason, str) or not reason for reason in reason_codes)
            or len(reason_codes) != len(set(reason_codes))
        ):
            raise ValueError("assessment must preserve unique reason codes.")
        if item["status"] == "eligible" and (
            reason_codes or any(state != "completed" for state in states.values())
        ):
            raise ValueError(
                "eligible pairs require two completed arms and no reasons."
            )
        if item["status"] == "incomplete" and reason_codes:
            raise ValueError(
                "incomplete pairs cannot have eligibility mismatch reasons."
            )
        if item["status"] == "ineligible" and not reason_codes:
            raise ValueError("ineligible pairs require at least one reason code.")
        reason_code_counts.update(reason_codes)
        if item.get("cluster_unit") != "fixture_id":
            raise ValueError("assessment must preserve the fixture clustering unit.")
        if item.get("attempts_are_independent_samples") is not False:
            raise ValueError("attempts must remain nested, not independent samples.")
        attempts = item.get("attempt_counts")
        if not isinstance(attempts, dict) or set(attempts) != {
            "baseline",
            "candidate",
        }:
            raise ValueError("assessment must preserve nested attempt counts by arm.")
        if any(type(count) is not int or count < 1 for count in attempts.values()):
            raise ValueError("attempt counts must be positive integers.")

        status_counts[item["status"]] += 1
        versions = item.get("versions")
        if not isinstance(versions, dict) or set(versions) != {
            "baseline",
            "candidate",
        }:
            raise ValueError("assessment must preserve both arm version identities.")
        for arm in ("baseline", "candidate"):
            version = versions[arm]
            if not isinstance(version, dict) or set(version) != {
                "harness_version",
                "harness_commit",
                "model_id",
                "model_version",
            }:
                raise ValueError("assessment contains an invalid arm version identity.")
            if any(
                not isinstance(value, str) or not value for value in version.values()
            ):
                raise ValueError(
                    "assessment version identities must be non-empty strings."
                )
        artifact_digests = item.get("artifact_digests")
        if not isinstance(artifact_digests, dict) or set(artifact_digests) != {
            "fixture_pack_sha256",
            "rubric_sha256",
            "protocol_sha256",
        }:
            raise ValueError("assessment must preserve all verified artifact digests.")
        for field, digest in artifact_digests.items():
            _digest(digest, field)
        identity = (
            item.get("cohort_id"),
            tuple(sorted(artifact_digests.items())),
            item.get("comparison_axis"),
        )
        _identifier(identity[0], "cohort_id")
        _choice(identity[2], {"harness", "model"}, "comparison_axis")
        baseline_version = versions["baseline"]
        candidate_version = versions["candidate"]
        harness_identity = ("harness_version", "harness_commit")
        model_identity = ("model_id", "model_version")
        if item["status"] != "ineligible":
            if identity[2] == "harness":
                if any(
                    baseline_version[field] != candidate_version[field]
                    for field in model_identity
                ):
                    raise ValueError(
                        "harness comparison changed the non-comparison model identity."
                    )
                if all(
                    baseline_version[field] == candidate_version[field]
                    for field in harness_identity
                ):
                    raise ValueError(
                        "harness comparison did not change its declared identity."
                    )
            else:
                if any(
                    baseline_version[field] != candidate_version[field]
                    for field in harness_identity
                ):
                    raise ValueError(
                        "model comparison changed the non-comparison harness identity."
                    )
                if all(
                    baseline_version[field] == candidate_version[field]
                    for field in model_identity
                ):
                    raise ValueError(
                        "model comparison did not change its declared identity."
                    )
        if summary_identity is None:
            summary_identity = identity
        elif identity != summary_identity:
            raise ValueError(
                "a summary cannot mix cohorts, artifact versions or comparison axes."
            )
        version_pairs.append(
            {
                "pair_id": pair_id,
                "baseline": dict(versions["baseline"]),
                "candidate": dict(versions["candidate"]),
            }
        )

        for arm, state in states.items():
            arm_state_counts[arm][state] += 1
            reason = terminal_reasons[arm]
            if reason is not None:
                arm_terminal_reason_counts[arm][reason] += 1
            attempts_seen += attempts[arm]
        if item["status"] == "eligible":
            eligible_fixtures.add(fixture_id)
        order_assignment = item.get("order_assignment")
        cache_condition = item.get("cache_condition")
        _choice(
            order_assignment,
            {"baseline_first", "candidate_first"},
            "assessment.order_assignment",
        )
        _choice(cache_condition, {"cold", "warm"}, "assessment.cache_condition")
        fixture_cell = (fixture_id, cache_condition, order_assignment)
        if fixture_cell in seen_fixture_cells:
            raise ValueError(
                "each fixture may have only one pair per preregistered cache/order cell."
            )
        seen_fixture_cells.add(fixture_cell)
        order_counts[order_assignment] += 1
        cache_counts[cache_condition] += 1
        cache_status_counts[cache_condition][item["status"]] += 1
        condition_cell = f"{cache_condition}/{order_assignment}"
        condition_cell_counts[condition_cell] += 1
        condition_cell_status_counts[condition_cell][item["status"]] += 1

    return {
        "schema_version": "aiobs.pair-eligibility-summary.v1",
        "evidence_class": "derived",
        "scope": "supplied pair assessments only; cohort completeness is not asserted; eligibility counts are stratified by cache and order condition",
        "cohort_id": summary_identity[0] if summary_identity else None,
        "artifact_digests": dict(summary_identity[1]) if summary_identity else None,
        "comparison_axis": summary_identity[2] if summary_identity else None,
        "compared_versions_by_pair": version_pairs,
        "compared_versions_are_uniform": (
            None
            if not version_pairs
            else len(
                {
                    (
                        tuple(sorted(pair["baseline"].items())),
                        tuple(sorted(pair["candidate"].items())),
                    )
                    for pair in version_pairs
                }
            )
            == 1
        ),
        "supplied_pair_count": len(assessments),
        "status_counts": {
            status: status_counts[status]
            for status in ("eligible", "ineligible", "incomplete")
        },
        "order_assignment_counts": {
            assignment: order_counts[assignment]
            for assignment in ("baseline_first", "candidate_first")
        },
        "cache_condition_counts": {
            condition: cache_counts[condition] for condition in ("cold", "warm")
        },
        "status_counts_by_cache_condition": {
            condition: {
                status: cache_status_counts[condition][status]
                for status in ("eligible", "ineligible", "incomplete")
            }
            for condition in ("cold", "warm")
        },
        "condition_cell_counts": {
            cell: condition_cell_counts[cell] for cell in condition_cells
        },
        "condition_cell_status_counts": {
            cell: {
                status: condition_cell_status_counts[cell][status]
                for status in ("eligible", "ineligible", "incomplete")
            }
            for cell in condition_cells
        },
        "eligibility_reason_code_counts": dict(sorted(reason_code_counts.items())),
        "arm_state_counts": {
            arm: {state: arm_state_counts[arm][state] for state in sorted(_RUN_STATES)}
            for arm in ("baseline", "candidate")
        },
        "arm_terminal_reason_counts": {
            arm: dict(sorted(arm_terminal_reason_counts[arm].items()))
            for arm in ("baseline", "candidate")
        },
        "eligible_fixture_cluster_count": len(eligible_fixtures),
        "cluster_unit": "fixture_id",
        "attempt_count_descriptive_only": attempts_seen,
        "attempts_are_independent_samples": False,
        "effect_size": {
            "state": "unavailable",
            "reason": "pair-eligibility summaries contain no scored outcome values",
        },
        "uncertainty": {
            "state": "unavailable",
            "reason": "no inferential estimator is applied to supplied eligibility records",
        },
    }
