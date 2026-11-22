"""Descriptive paired summaries for already-scored synthetic fixture runs.

This module performs no model inference, network or filesystem access, and does
not infer task acceptance. It consumes only validated pair classifications,
rubric-shaped labels, and explicit synthetic resource values.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from statistics import stdev
from typing import Any

from integrations import benchmark_protocol

_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,96}\Z")
_CURRENCY = re.compile(r"[A-Z]{3}\Z")
_REFERENCE_PROVENANCE = (
    "curated synthetic target labels; not independently validated against real work"
)
_RESOURCE_UNITS = {
    "elapsed_seconds": "seconds",
    "tokens": "tokens",
    "cost": "declared_currency",
}
_RESOURCE_FIELDS = set(_RESOURCE_UNITS)
_METRIC_UNAVAILABILITY = {
    "X1": "Attested synthetic judgment agreement is not independent task acceptance.",
    "X2": "No independent pass/fail task outcomes exist for transition classification.",
    "X3": "Synthetic resource deltas are diagnostics, not source-backed metric delivery; accepted-task subsets lack independent acceptance outcomes.",
    "X4": "No independent accepted-task outcomes or complete production resource cohort exist.",
    "X5": "The fixed fixture IDs do not evidence task-type, tool, flow, agent, or memory-scenario coverage.",
    "X7": "Synthetic repeat variability is descriptive fixture behavior, not validated metric delivery or population uncertainty.",
}


def _identifier(value: Any, field: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field} must be an opaque identifier.")


def _validate_schema(
    allowed_labels: Mapping[str, Sequence[str]],
    reference_labels: Mapping[str, Any],
) -> tuple[dict[str, tuple[str, ...]], dict[str, dict[str, str]]]:
    if not isinstance(allowed_labels, Mapping) or not allowed_labels:
        raise ValueError("allowed_labels must define a non-empty rubric schema.")

    schema: dict[str, tuple[str, ...]] = {}
    for field, categories in allowed_labels.items():
        _identifier(field, "rubric field")
        if (
            not isinstance(categories, (list, tuple))
            or not categories
            or any(not isinstance(value, str) or not value for value in categories)
            or len(categories) != len(set(categories))
        ):
            raise ValueError("rubric categories must be unique non-empty strings.")
        schema[field] = tuple(categories)

    if not isinstance(reference_labels, Mapping) or not reference_labels:
        raise ValueError("reference_labels must contain synthetic fixture targets.")

    references: dict[str, dict[str, str]] = {}
    for fixture_id, labels in reference_labels.items():
        _identifier(fixture_id, "reference fixture_id")
        if not isinstance(labels, dict) or set(labels) != set(schema):
            raise ValueError("reference labels must match the exact rubric fields.")
        if any(
            not isinstance(labels[field], str) or labels[field] not in schema[field]
            for field in schema
        ):
            raise ValueError("reference labels must use declared rubric categories.")
        references[fixture_id] = dict(labels)
    return schema, references


def _validate_observations(
    pair_observations: list[Any], pair_ids: set[str]
) -> dict[str, dict[str, Any]]:
    if not isinstance(pair_observations, list):
        raise TypeError("pair_observations must be a list.")

    observations: dict[str, dict[str, Any]] = {}
    for item in pair_observations:
        if not isinstance(item, dict) or set(item) != {"pair_id", "arms"}:
            raise ValueError(
                "each pair observation must contain only pair_id and arms."
            )
        pair_id = item["pair_id"]
        _identifier(pair_id, "pair_observation.pair_id")
        if pair_id not in pair_ids:
            raise ValueError("pair observation does not match a classified pair.")
        if pair_id in observations:
            raise ValueError("pair observations must have unique pair_id values.")
        arms = item["arms"]
        if not isinstance(arms, dict) or set(arms) != {"baseline", "candidate"}:
            raise ValueError("pair observation must preserve both arms.")
        for arm, result in arms.items():
            if not isinstance(result, dict) or set(result) != {
                "assessment",
                "resources",
            }:
                raise ValueError(
                    f"{arm} observation must contain only assessment and resources."
                )
            resources = result["resources"]
            if resources is not None and (
                not isinstance(resources, dict)
                or not set(resources).issubset(_RESOURCE_FIELDS)
            ):
                raise ValueError(f"{arm} resources contain unsupported fields.")
        observations[pair_id] = arms
    return observations


def _score_assessment(
    assessment: Any,
    reference: Mapping[str, str],
    schema: Mapping[str, Sequence[str]],
) -> tuple[dict[str, Any] | None, str | None]:
    if assessment is None:
        return None, "missing_assessment"
    if (
        not isinstance(assessment, dict)
        or set(assessment) != set(schema)
        or any(
            not isinstance(assessment[field], str)
            or assessment[field] not in schema[field]
            for field in schema
        )
    ):
        return None, "invalid_assessment_schema"

    matched = sum(assessment[field] == reference[field] for field in schema)
    total = len(schema)
    return {
        "matched_fields": matched,
        "total_fields": total,
        "agreement_fraction": matched / total,
    }, None


def _resource_value(
    resources: Any, field: str
) -> tuple[str, Any, dict[str, str] | None]:
    if resources is None or field not in resources or resources[field] is None:
        return "missing", None, None

    value = resources[field]
    if field == "elapsed_seconds":
        if (
            type(value) not in (int, float)
            or value < 0
            or (type(value) is float and not math.isfinite(value))
        ):
            return "invalid", None, None
        return "observed", value, None

    if field == "tokens":
        if (
            not isinstance(value, dict)
            or set(value) != {"value", "basis"}
            or type(value["value"]) is not int
            or value["value"] < 0
            or not isinstance(value["basis"], str)
            or not _IDENTIFIER.fullmatch(value["basis"])
        ):
            return "invalid", None, None
        return "observed", value["value"], {"basis": value["basis"]}

    if not isinstance(value, dict) or set(value) != {"amount", "currency", "basis"}:
        return "invalid", None, None
    amount = value["amount"]
    currency = value["currency"]
    basis = value["basis"]
    if (
        type(amount) not in (int, float)
        or amount < 0
        or (type(amount) is float and not math.isfinite(amount))
        or not isinstance(currency, str)
        or not _CURRENCY.fullmatch(currency)
        or not isinstance(basis, str)
        or not _IDENTIFIER.fullmatch(basis)
    ):
        return "invalid", None, None
    return "observed", amount, {"currency": currency, "basis": basis}


def _summarize_resource(
    field: str,
    assessments: list[dict[str, Any]],
    observations: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    eligible = [item for item in assessments if item["status"] == "eligible"]
    deltas: list[dict[str, Any]] = []
    unpaired: list[dict[str, Any]] = []
    missing_arm_counts: Counter[str] = Counter()
    invalid_arm_counts: Counter[str] = Counter()
    pair_reason_counts: Counter[str] = Counter()
    metadata_seen: set[tuple[tuple[str, str], ...]] = set()

    for item in eligible:
        pair_id = item["pair_id"]
        arms = observations.get(pair_id)
        values: dict[str, Any] = {}
        metadata: dict[str, dict[str, str] | None] = {}
        reasons: list[str] = []
        for arm in ("baseline", "candidate"):
            resources = None if arms is None else arms[arm]["resources"]
            state, value, details = _resource_value(resources, field)
            if state != "observed":
                reasons.append(f"{state}_{arm}")
                if state == "missing":
                    missing_arm_counts[arm] += 1
                else:
                    invalid_arm_counts[arm] += 1
            else:
                values[arm] = value
                metadata[arm] = details

        if not reasons and field == "tokens":
            baseline_metadata = metadata["baseline"] or {}
            candidate_metadata = metadata["candidate"] or {}
            if baseline_metadata.get("basis") != candidate_metadata.get("basis"):
                reasons.append("token_basis_mismatch")
        elif not reasons and field == "cost":
            baseline_metadata = metadata["baseline"] or {}
            candidate_metadata = metadata["candidate"] or {}
            if baseline_metadata.get("currency") != candidate_metadata.get("currency"):
                reasons.append("currency_mismatch")
            if baseline_metadata.get("basis") != candidate_metadata.get("basis"):
                reasons.append("cost_basis_mismatch")

        if reasons:
            pair_reason_counts.update(reasons)
            unpaired.append({"pair_id": pair_id, "reason_codes": reasons})
            continue

        details = metadata["candidate"]
        metadata_seen.add(tuple(sorted((details or {}).items())))
        delta: dict[str, Any] = {
            "pair_id": pair_id,
            "candidate_minus_baseline": values["candidate"] - values["baseline"],
        }
        if details:
            delta.update(details)
        deltas.append(delta)

    eligible_count = len(eligible)
    paired_count = len(deltas)
    complete = eligible_count > 0 and paired_count == eligible_count
    missing_pair_count = sum(
        any(reason.startswith("missing_") for reason in item["reason_codes"])
        for item in unpaired
    )
    invalid_pair_count = sum(
        any(reason.startswith("invalid_") for reason in item["reason_codes"])
        for item in unpaired
    )
    incompatible_basis_pair_count = sum(
        any(
            reason
            in {"token_basis_mismatch", "currency_mismatch", "cost_basis_mismatch"}
            for reason in item["reason_codes"]
        )
        for item in unpaired
    )
    total_reason: str | None = None
    total: int | float | None = None

    if not eligible_count:
        total_reason = "no_eligible_pairs"
    elif paired_count != eligible_count:
        total_reason = "incomplete_resource_coverage"
    elif len(metadata_seen) > 1:
        total_reason = "mixed_resource_basis"
    else:
        total = sum(delta["candidate_minus_baseline"] for delta in deltas)
        if type(total) is float and not math.isfinite(total):
            total = None
            total_reason = "total_out_of_range"

    basis: dict[str, str] | None = None
    if len(metadata_seen) == 1:
        basis = dict(next(iter(metadata_seen)))

    return {
        "evidence_class": "synthetic",
        "unit": _RESOURCE_UNITS[field],
        "eligible_pair_count": eligible_count,
        "paired_observation_count": paired_count,
        "unpaired_pair_count": eligible_count - paired_count,
        "missing_pair_count": missing_pair_count,
        "invalid_pair_count": invalid_pair_count,
        "incompatible_basis_pair_count": incompatible_basis_pair_count,
        "missing_arm_counts": {
            arm: missing_arm_counts[arm] for arm in ("baseline", "candidate")
        },
        "invalid_arm_counts": {
            arm: invalid_arm_counts[arm] for arm in ("baseline", "candidate")
        },
        "unpaired_reason_counts": dict(sorted(pair_reason_counts.items())),
        "unpaired_pairs": unpaired,
        "paired_deltas": deltas,
        "complete_pair_coverage": complete,
        "total_candidate_minus_baseline": total,
        "total_state": "available_descriptive" if total is not None else "unavailable",
        "total_unavailable_reason": total_reason,
        "basis": basis,
    }


def _summarize_repeat_variability(
    assessments: list[dict[str, Any]], pair_results: Mapping[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in assessments:
        groups[(item["fixture_id"], item["cache_condition"])].append(item)

    output: list[dict[str, Any]] = []
    for (fixture_id, cache_condition), items in sorted(groups.items()):
        scored = [
            item
            for item in items
            if pair_results[item["pair_id"]]["scoring_state"] == "scored"
        ]
        eligible_versions = {
            (
                tuple(sorted(item["versions"]["baseline"].items())),
                tuple(sorted(item["versions"]["candidate"].items())),
            )
            for item in items
            if item["status"] == "eligible"
        }
        value = {
            "fixture_id": fixture_id,
            "cache_condition": cache_condition,
            "supplied_pair_count": len(items),
            "scored_replicate_count": len(scored),
            "cluster_unit": "fixture_id",
            "replicate_unit": "pair_id; retries remain nested attempts",
            "unit": "agreement_fraction",
            "state": "unavailable",
            "reason": "fewer_than_two_scored_replicates",
            "mean_paired_delta": None,
            "range": None,
            "sample_standard_deviation": None,
            "versions_are_frozen": len(eligible_versions) == 1,
            "uncertainty": {
                "state": "unavailable",
                "reason": "descriptive synthetic repeat variability is not population inference",
            },
        }
        if len(scored) >= 2:
            if len(eligible_versions) != 1:
                value["reason"] = "comparison_versions_not_frozen"
            else:
                deltas = [
                    pair_results[item["pair_id"]]["candidate_minus_baseline"]
                    for item in scored
                ]
                value.update(
                    {
                        "state": "descriptive_only",
                        "reason": None,
                        "mean_paired_delta": sum(deltas) / len(deltas),
                        "range": max(deltas) - min(deltas),
                        "sample_standard_deviation": stdev(deltas),
                    }
                )
        output.append(value)
    return output


def summarize_synthetic_pairs(
    pair_assessments: list[dict[str, Any]],
    pair_observations: list[Any],
    reference_labels: Mapping[str, Any],
    allowed_labels: Mapping[str, Sequence[str]],
    *,
    max_replicates_per_fixture: int,
) -> dict[str, Any]:
    """Summarize paired agreement and explicit synthetic resources offline.

    Pair eligibility is validated before scoring. Missing or malformed arm
    judgments remain unresolved; ``unknown`` is a valid rubric category. Resource
    totals are emitted only with complete coverage of all eligible pairs and a
    consistent declared basis. No version ranking or metric delivery is emitted.
    """
    if not isinstance(pair_assessments, list):
        raise TypeError("pair_assessments must be a list.")
    schema, references = _validate_schema(allowed_labels, reference_labels)
    eligibility = benchmark_protocol.summarize_pair_eligibility(
        pair_assessments,
        max_replicates_per_fixture=max_replicates_per_fixture,
    )
    assessment_by_id = {item["pair_id"]: item for item in pair_assessments}
    if any(item["fixture_id"] not in references for item in pair_assessments):
        raise ValueError(
            "every classified fixture_id needs a synthetic reference label."
        )
    observations = _validate_observations(pair_observations, set(assessment_by_id))

    pair_results: dict[str, dict[str, Any]] = {}
    pair_rows: list[dict[str, Any]] = []
    scored_pair_count = 0
    unresolved_pair_count = 0
    unresolved_arm_counts: Counter[str] = Counter()

    for item in pair_assessments:
        pair_id = item["pair_id"]
        state = item["status"]
        row: dict[str, Any] = {
            "pair_id": pair_id,
            "fixture_id": item["fixture_id"],
            "cache_condition": item["cache_condition"],
            "status": state,
            "eligibility_reason_codes": list(item["reason_codes"]),
            "arm_states": dict(item["arm_states"]),
            "arm_terminal_reasons": dict(item["arm_terminal_reasons"]),
            "attempt_counts": dict(item["attempt_counts"]),
            "attempts_are_independent_samples": False,
            "cluster_unit": "fixture_id",
            "scoring_state": "unavailable",
            "reason_codes": [],
            "baseline_agreement": None,
            "candidate_agreement": None,
            "candidate_minus_baseline": None,
        }
        if state != "eligible":
            row["reason_codes"] = [f"pair_{state}"]
            pair_results[pair_id] = row
            pair_rows.append(row)
            continue

        arms = observations.get(pair_id)
        reasons: list[str] = []
        arm_scores: dict[str, dict[str, Any] | None] = {}
        for arm in ("baseline", "candidate"):
            prediction = None if arms is None else arms[arm]["assessment"]
            score, reason = _score_assessment(
                prediction, references[item["fixture_id"]], schema
            )
            arm_scores[arm] = score
            if reason is not None:
                reasons.append(f"{reason}_{arm}")
                unresolved_arm_counts[arm] += 1

        if reasons:
            unresolved_pair_count += 1
            row.update({"scoring_state": "unresolved", "reason_codes": reasons})
        else:
            scored_pair_count += 1
            baseline_agreement = arm_scores["baseline"]["agreement_fraction"]
            candidate_agreement = arm_scores["candidate"]["agreement_fraction"]
            row.update(
                {
                    "scoring_state": "scored",
                    "baseline_agreement": baseline_agreement,
                    "candidate_agreement": candidate_agreement,
                    "candidate_minus_baseline": candidate_agreement
                    - baseline_agreement,
                    "matched_fields": {
                        arm: arm_scores[arm]["matched_fields"]
                        for arm in ("baseline", "candidate")
                    },
                    "total_fields_per_arm": len(schema),
                }
            )
        pair_results[pair_id] = row
        pair_rows.append(row)

    eligible_count = eligibility["status_counts"]["eligible"]
    resources = {
        field: _summarize_resource(field, pair_assessments, observations)
        for field in ("elapsed_seconds", "tokens", "cost")
    }
    repeat_variability = _summarize_repeat_variability(pair_assessments, pair_results)

    return {
        "schema_version": "aiobs.paired-comparison-summary.v1",
        "evidence_class": "derived",
        "evidence_scope": "synthetic_fixture_only",
        "judgment_evidence_class": "judged",
        "reference_evidence_class": "attested",
        "reference_provenance": _REFERENCE_PROVENANCE,
        "metric_delivery_advanced": False,
        "scope": "supplied synthetic pair assessments only; cohort completeness is not asserted",
        "pair_eligibility": eligibility,
        "paired_judgment_agreement": {
            "outcome": "candidate minus baseline exact-field agreement fraction against attested synthetic labels",
            "eligible_pair_count": eligible_count,
            "scored_pair_count": scored_pair_count,
            "unresolved_pair_count": unresolved_pair_count,
            "unresolved_arm_counts": {
                arm: unresolved_arm_counts[arm] for arm in ("baseline", "candidate")
            },
            "pair_results": pair_rows,
            "repeat_variability_by_fixture_cache": repeat_variability,
            "uncertainty": {
                "state": "unavailable",
                "reason": "three-fixture synthetic agreement is descriptive; no population estimator is applied",
            },
        },
        "synthetic_resource_deltas": resources,
        "version_ranking": {
            "state": "unavailable",
            "ranked": False,
            "reason": "Synthetic fixture summaries are not production evidence; missing resource totals also preclude complete-cohort comparisons.",
        },
        "metric_availability": {
            metric_id: {"state": "unavailable", "reason": reason}
            for metric_id, reason in _METRIC_UNAVAILABILITY.items()
        },
        "limitations": [
            "Attested synthetic targets are not independently validated task outcomes or acceptance labels.",
            "A valid unknown rubric label is scored as a category; missing or malformed outputs remain unresolved.",
            "Resource values are synthetic diagnostics; missing values are never treated as zero.",
            "Retries remain nested attempts; repeated pair records are clustered by fixture_id.",
            "No metric delivery state, accepted-task yield, production ranking, or population uncertainty is established.",
        ],
    }
