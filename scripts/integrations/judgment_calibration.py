"""Offline agreement summaries for versioned synthetic judgment fixtures."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

_CASE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_FIELD = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_REFERENCE_PROVENANCE = (
    "curated synthetic target labels; not independently validated against real work"
)


def _metadata_text(value: Any, field: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 256
        or any(ord(char) < 32 for char in value)
    ):
        raise ValueError(f"{field} must be bounded, non-empty metadata text.")


def _case_id(value: Any) -> None:
    if not isinstance(value, str) or not _CASE_ID.fullmatch(value):
        raise ValueError("case_id must be a declared opaque fixture identifier.")


def _schema(allowed_labels: Mapping[str, Sequence[str]]) -> dict[str, tuple[str, ...]]:
    if not isinstance(allowed_labels, Mapping) or not allowed_labels:
        raise ValueError("allowed_labels must define a non-empty rubric schema.")

    schema: dict[str, tuple[str, ...]] = {}
    for field, labels in allowed_labels.items():
        if not isinstance(field, str) or not _FIELD.fullmatch(field):
            raise ValueError("rubric fields must be stable identifiers.")
        if (
            not isinstance(labels, (list, tuple))
            or not labels
            or any(not isinstance(label, str) or not label for label in labels)
            or len(labels) != len(set(labels))
        ):
            raise ValueError(f"{field} must have unique, non-empty label categories.")
        schema[field] = tuple(labels)
    return schema


def _references(
    labels: Mapping[str, Any], schema: Mapping[str, Sequence[str]]
) -> dict[str, dict[str, str]]:
    if not isinstance(labels, Mapping) or not labels:
        raise ValueError("reference labels must contain the declared synthetic cases.")

    validated: dict[str, dict[str, str]] = {}
    for fixture_id, value in labels.items():
        _case_id(fixture_id)
        if not isinstance(value, dict) or set(value) != set(schema):
            raise ValueError("each reference case must match the exact rubric fields.")
        if any(
            not isinstance(value[field], str) or value[field] not in schema[field]
            for field in schema
        ):
            raise ValueError("reference labels must use declared rubric categories.")
        validated[fixture_id] = dict(value)
    return validated


def summarize_calibration(
    predictions: list[Any],
    reference_labels: Mapping[str, Any],
    allowed_labels: Mapping[str, Sequence[str]],
    *,
    model_id: str,
    rubric_id: str,
    rubric_version: str,
    fixture_pack_id: str,
    fixture_pack_version: str,
) -> dict[str, Any]:
    """Compare supplied labels with attested synthetic targets without inference.

    Missing and malformed assessments are unresolved. A valid ``unknown`` label
    remains a scored rubric category and is counted separately as abstention.
    """
    if not isinstance(predictions, list):
        raise TypeError("predictions must be a list of case assessments.")

    for value, field in (
        (model_id, "model_id"),
        (rubric_id, "rubric_id"),
        (rubric_version, "rubric_version"),
        (fixture_pack_id, "fixture_pack_id"),
        (fixture_pack_version, "fixture_pack_version"),
    ):
        _metadata_text(value, field)

    schema = _schema(allowed_labels)
    references = _references(reference_labels, schema)
    supplied: dict[str, Any] = {}
    for item in predictions:
        if not isinstance(item, dict) or set(item) != {"case_id", "assessment"}:
            raise ValueError(
                "each prediction must contain only case_id and assessment."
            )
        fixture_id = item["case_id"]
        _case_id(fixture_id)
        if fixture_id not in references:
            raise ValueError("prediction case_id is not in the reference fixture pack.")
        if fixture_id in supplied:
            raise ValueError("prediction case_id values must be unique.")
        supplied[fixture_id] = item["assessment"]

    valid: dict[str, dict[str, str]] = {}
    unresolved_reasons: Counter[str] = Counter()
    for fixture_id in references:
        if fixture_id not in supplied:
            unresolved_reasons["missing_prediction"] += 1
            continue
        assessment = supplied[fixture_id]
        if (
            not isinstance(assessment, dict)
            or set(assessment) != set(schema)
            or any(
                not isinstance(assessment[field], str)
                or assessment[field] not in schema[field]
                for field in schema
            )
        ):
            unresolved_reasons["invalid_assessment_schema"] += 1
            continue
        valid[fixture_id] = dict(assessment)

    field_counts: dict[str, dict[str, Any]] = {}
    matched_fields = 0
    disagreement_count = 0
    explicit_unknown_count = 0
    reference_unknown_count = 0
    scored_fields = len(valid) * len(schema)
    unresolved_fields = (len(references) - len(valid)) * len(schema)

    for field, categories in schema.items():
        matched = 0
        judgment_counts: Counter[str] = Counter()
        reference_counts: Counter[str] = Counter()
        disagreement_categories: Counter[tuple[str, str]] = Counter()
        for fixture_id, assessment in valid.items():
            judgment = assessment[field]
            reference = references[fixture_id][field]
            judgment_counts[judgment] += 1
            reference_counts[reference] += 1
            if judgment == "unknown":
                explicit_unknown_count += 1
            if reference == "unknown":
                reference_unknown_count += 1
            if judgment == reference:
                matched += 1
            else:
                disagreement_categories[(reference, judgment)] += 1

        mismatched = sum(disagreement_categories.values())
        matched_fields += matched
        disagreement_count += mismatched
        field_counts[field] = {
            "scored_fields": len(valid),
            "matched_fields": matched,
            "disagreement_count": mismatched,
            "unresolved_fields": len(references) - len(valid),
            "agreement_rate": matched / len(valid) if valid else None,
            "judgment_category_counts": {
                category: judgment_counts[category] for category in categories
            },
            "reference_category_counts": {
                category: reference_counts[category] for category in categories
            },
            "disagreement_categories": [
                {"reference": reference, "judgment": judgment, "count": count}
                for (reference, judgment), count in sorted(
                    disagreement_categories.items()
                )
            ],
        }

    total_fields = len(references) * len(schema)
    return {
        "schema_version": "aiobs.judgment-calibration.v1",
        "metric_id": "X6",
        "metric_delivery_advanced": False,
        "evidence_class": "derived",
        "judgment_evidence_class": "local_model_judgment",
        "reference_evidence_class": "attested",
        "actor": "local_model",
        "model_id": model_id,
        "rubric": {"id": rubric_id, "version": rubric_version},
        "fixture_pack": {"id": fixture_pack_id, "version": fixture_pack_version},
        "reference_provenance": _REFERENCE_PROVENANCE,
        "sample_scope": "bundled synthetic fixtures only; not a probability sample",
        "case_counts": {
            "reference_cases": len(references),
            "supplied_predictions": len(supplied),
            "scored_cases": len(valid),
            "unresolved_cases": len(references) - len(valid),
            "unresolved_reason_counts": {
                reason: unresolved_reasons[reason]
                for reason in ("missing_prediction", "invalid_assessment_schema")
            },
        },
        "matched_fields": matched_fields,
        "total_fields": total_fields,
        "scored_fields": scored_fields,
        "disagreement_count": disagreement_count,
        "unresolved_fields": unresolved_fields,
        "agreement_rate": matched_fields / scored_fields if scored_fields else None,
        "explicit_unknown_judgment_count": explicit_unknown_count,
        "unknown_reference_label_count": reference_unknown_count,
        "field_calibration": field_counts,
        "uncertainty": {
            "state": "unavailable",
            "reason": "Synthetic fixture agreement is descriptive; no inferential estimator is applied.",
        },
        "limitations": [
            "Curated synthetic targets are attested fixtures, not independently validated truth.",
            "Judgments and agreement do not establish task acceptance or production quality.",
            "This result does not advance X6 metric delivery state.",
        ],
        "note": "Synthetic calibration only; not a validated benchmark.",
    }
