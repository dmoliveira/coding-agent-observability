"""Read explicitly selected, structural task attestations; never discover or write data."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import stat
from collections import defaultdict
from pathlib import Path
from typing import Any

MAX_BYTES = 1024 * 1024
MAX_TASKS = 1000
MAX_BINDINGS = 16
ID = re.compile(r"[A-Za-z0-9_-]{1,160}\Z")
TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})\Z"
)
KINDS = ("epic", "task", "subtask")
STATUSES = (
    "not_started",
    "in_progress",
    "completed",
    "blocked",
    "failed",
    "interrupted",
    "canceled",
    "unknown",
)
VALIDATIONS = ("passed", "failed", "not_run", "unknown")
PROVENANCE = ("human", "agent", "automation", "unknown")
TASK_FIELDS = {
    "id",
    "kind",
    "parent_id",
    "status",
    "validation",
    "attested_by",
    "bindings",
}


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Task evidence contains duplicate JSON keys.")
        result[key] = value
    return result


def _identifier(value: Any) -> bool:
    return isinstance(value, str) and ID.fullmatch(value) is not None


def validate(value: Any) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "as_of", "tasks"}
        or value["schema_version"] != "1.0"
    ):
        raise ValueError("Task evidence has an invalid schema or version.")
    stamp = value["as_of"]
    try:
        parsed = (
            dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if isinstance(stamp, str) and TIMESTAMP.fullmatch(stamp)
            else None
        )
        if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        normalized_stamp = parsed.astimezone(dt.timezone.utc).isoformat()
    except (ValueError, OverflowError):
        raise ValueError(
            "Task evidence as_of must be an ISO timestamp with timezone."
        ) from None
    tasks = value["tasks"]
    if not isinstance(tasks, list) or len(tasks) > MAX_TASKS:
        raise ValueError("Task evidence must contain at most 1000 tasks.")
    ids: set[str] = set()
    parents: dict[str, str | None] = {}
    for task in tasks:
        if not isinstance(task, dict) or set(task) != TASK_FIELDS:
            raise ValueError("Task evidence contains an invalid task record.")
        task_id = task["id"]
        if not _identifier(task_id) or task_id in ids:
            raise ValueError("Task evidence contains an invalid or duplicate task ID.")
        ids.add(task_id)
        parent = task["parent_id"]
        if parent is not None and not _identifier(parent):
            raise ValueError("Task evidence contains an invalid parent ID.")
        parents[task_id] = parent
        if any(
            not isinstance(task[key], str) or task[key] not in choices
            for key, choices in (
                ("kind", KINDS),
                ("status", STATUSES),
                ("validation", VALIDATIONS),
                ("attested_by", PROVENANCE),
            )
        ):
            raise ValueError("Task evidence contains an unrecognized category.")
        bindings = task["bindings"]
        if not isinstance(bindings, list) or len(bindings) > MAX_BINDINGS:
            raise ValueError("Each task may bind at most 16 turns.")
        seen: set[tuple[str, str]] = set()
        for binding in bindings:
            if (
                not isinstance(binding, dict)
                or set(binding) != {"trace_id", "turn_id"}
                or not _identifier(binding["trace_id"])
                or not _identifier(binding["turn_id"])
            ):
                raise ValueError("Task evidence contains an invalid turn binding.")
            key = (binding["trace_id"], binding["turn_id"])
            if key in seen:
                raise ValueError("Task evidence contains a duplicate turn binding.")
            seen.add(key)
    # Parent links may point outside a partial snapshot, but cycles are invalid.
    checked: set[str] = set()
    for task_id in parents:
        path: set[str] = set()
        current: str | None = task_id
        while current in parents and current not in checked:
            if current in path:
                raise ValueError("Task evidence contains a cyclic hierarchy.")
            path.add(current)
            current = parents[current]
        checked.update(path)
    return {**value, "as_of": normalized_stamp}


def read_file(path: Path) -> dict[str, Any]:
    try:
        fd = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_size > MAX_BYTES
            ):
                raise ValueError(
                    "Task evidence must be a private regular file owned by the current user (maximum 1 MiB)."
                )
            raw = stream.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError("Task evidence exceeds the 1 MiB limit.")
            value = json.loads(raw, object_pairs_hook=_unique_object)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ValueError(
            "Could not safely read the selected task evidence file."
        ) from None
    return validate(value)


def binding_owners(value: dict[str, Any]) -> dict[tuple[str, str], set[str]]:
    owners: dict[tuple[str, str], set[str]] = defaultdict(set)
    for task in value["tasks"]:
        for binding in task["bindings"]:
            owners[(binding["trace_id"], binding["turn_id"])].add(task["id"])
    return dict(owners)


def summarize(
    value: dict[str, Any],
    turns: set[tuple[str, str]],
    include_identifiers: bool = False,
) -> dict[str, Any]:
    tasks = value["tasks"]
    ids = {task["id"] for task in tasks}
    bindings = binding_owners(value)
    unique = {key for key, owners in bindings.items() if len(owners) == 1}
    result: dict[str, Any] = {
        "evidence_class": "operator_supplied_attestation",
        "as_of": value["as_of"],
        "task_records": len(tasks),
        "by_kind": {key: sum(task["kind"] == key for task in tasks) for key in KINDS},
        "by_status": {
            key: sum(task["status"] == key for task in tasks) for key in STATUSES
        },
        "by_kind_and_status": {
            kind: {
                status: sum(
                    task["kind"] == kind and task["status"] == status for task in tasks
                )
                for status in STATUSES
            }
            for kind in KINDS
        },
        "by_validation": {
            key: sum(task["validation"] == key for task in tasks) for key in VALIDATIONS
        },
        "by_attested_by": {
            key: sum(task["attested_by"] == key for task in tasks) for key in PROVENANCE
        },
        "completed_records_with_pass_attested": sum(
            task["status"] == "completed" and task["validation"] == "passed"
            for task in tasks
        ),
        "completed_records_without_pass_attested": sum(
            task["status"] == "completed" and task["validation"] != "passed"
            for task in tasks
        ),
        "parent_links_outside_snapshot": sum(
            task["parent_id"] is not None and task["parent_id"] not in ids
            for task in tasks
        ),
        "tasks_without_bindings": sum(not task["bindings"] for task in tasks),
        "unique_bindings_matched": len(unique & turns),
        "unique_bindings_not_observed": len(unique - turns),
        "ambiguous_bindings": len(bindings) - len(unique),
        "limitations": [
            "Task records and their outcomes are supplied claims, not independently verified facts.",
            "Task snapshot counts are independent of the observation window and session filter and may be incomplete or stale.",
            "An exact ID match does not verify a claimed task relationship; absent turns may be outside the window.",
            "Bindings claimed by multiple tasks are ambiguous; no telemetry or tokens are attributed to them.",
        ],
    }
    if include_identifiers:
        result["tasks"] = [
            {
                **task,
                "bindings": [
                    {
                        **binding,
                        "match": "ambiguous"
                        if len(bindings[(binding["trace_id"], binding["turn_id"])]) > 1
                        else "observed_turn"
                        if (binding["trace_id"], binding["turn_id"]) in turns
                        else "not_observed",
                    }
                    for binding in task["bindings"]
                ],
            }
            for task in tasks
        ]
    return result
