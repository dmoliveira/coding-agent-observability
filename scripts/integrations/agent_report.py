"""Bounded structural OpenCode report; never request observation input or output."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import math
import re
import sys
import urllib.parse
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from integrations import agent_dashboard_definition as dashboard_spec
from integrations import span_dashboard, task_evidence

UTC = dt.timezone.utc
FIELDS = "basic,model,usage,metrics"
TOKEN_KEYS = ("input", "output", "total", "reasoning", "cache_read", "cache_write")
SELECTED = (
    "id",
    "traceId",
    "parentObservationId",
    "type",
    "name",
    "startTime",
    "endTime",
    "sessionId",
    "level",
    "isRootObservation",
    "model",
    "latency",
    "environment",
)
RUNTIME_NAMES = {
    "aiobs.execution.started",
    "aiobs.execution.succeeded",
    "aiobs.execution.failed",
    *dashboard_spec.INTERRUPTED,
    *(name for names in dashboard_spec.COMPACTION.values() for name in names),
    dashboard_spec.PERMISSION_ASK,
    *dashboard_spec.PERMISSION_REPLIES,
}


def timestamp(value: str) -> dt.datetime:
    try:
        result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise ValueError("Timestamps must be ISO 8601 with a timezone.") from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Timestamps must include a timezone.")
    return result.astimezone(UTC)


def scalar_number(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value


def select(row: Any) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ValueError("Langfuse returned an invalid observation.")  # noqa: TRY004
    selected = {key: row.get(key) for key in SELECTED}
    for key in ("id", "traceId"):
        if (
            not isinstance(selected[key], str)
            or not selected[key]
            or len(selected[key]) > 512
        ):
            raise ValueError(
                "Langfuse returned an observation without a stable identity."
            )
    for key in (
        "parentObservationId",
        "type",
        "name",
        "startTime",
        "endTime",
        "sessionId",
        "model",
        "environment",
    ):
        value = selected[key]
        if not isinstance(value, str) or len(value) > 512:
            selected[key] = None
    if selected["level"] not in ("DEBUG", "DEFAULT", "WARNING", "ERROR"):
        selected["level"] = None
    if not isinstance(selected["isRootObservation"], bool):
        selected["isRootObservation"] = None
    selected["latency"] = scalar_number(selected["latency"])
    usage = row.get("usageDetails")
    selected["usageDetails"] = {
        key: scalar_number(usage.get(key)) if isinstance(usage, dict) else None
        for key in TOKEN_KEYS
    }
    # Never retain statusMessage, metadata, input, output, or arbitrary API fields.
    return selected


def fetch_observations(
    client: span_dashboard.Client,
    start: dt.datetime,
    end: dt.datetime,
    maximum: int,
    session_id: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    params: dict[str, Any] = {
        "fields": FIELDS,
        "limit": min(250, maximum),
        "fromStartTime": start.isoformat(),
        "toStartTime": end.isoformat(),
    }
    if session_id is not None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", session_id):
            raise ValueError("Invalid OpenCode session ID.")
        params["sessionId"] = session_id
    result: dict[tuple[str, str], dict[str, Any]] = {}
    cursors: set[str] = set()
    duplicates = 0
    for _ in range(1000):
        page = client.request(
            "GET", "/api/public/v2/observations?" + urllib.parse.urlencode(params)
        )
        rows, meta = page.get("data"), page.get("meta")
        if not isinstance(rows, list) or not isinstance(meta, dict):
            raise ValueError("Langfuse returned invalid observation pagination.")  # noqa: TRY004
        for item in rows:
            row = select(item)
            key = (row["traceId"], row["id"])
            if key in result:
                if result[key] != row:
                    raise ValueError(
                        "An observation changed during the scan; retry the report."
                    )
                duplicates += 1
            else:
                result[key] = row
            if len(result) > maximum:
                raise ValueError(
                    "Observation limit exceeded; narrow the window or raise the limit."
                )
        cursor = meta.get("cursor")
        if cursor is None:
            return list(result.values()), duplicates
        if (
            not isinstance(cursor, str)
            or not cursor
            or len(cursor) > 4096
            or cursor in cursors
        ):
            raise ValueError(
                "Langfuse returned an invalid or repeated observation cursor."
            )
        if len(result) >= maximum:
            raise ValueError(
                "Observation limit reached before the last page; narrow the window or raise the limit."
            )
        cursors.add(cursor)
        params["cursor"] = cursor
    raise ValueError("Observation page limit reached; narrow the window.")


def kind(row: dict[str, Any]) -> str:
    if (
        row.get("type") == "EVENT"
        and row.get("environment") == dashboard_spec.RUNTIME_ENVIRONMENT
        and row.get("name") in RUNTIME_NAMES
    ):
        return "runtime"
    if row.get("name") == "opencode.turn" and row.get("type") == "AGENT":
        return "turn"
    if (
        row.get("name") in ("opencode.generation", "opencode.generation.failed")
        and row.get("type") == "GENERATION"
    ):
        return "generation"
    if row.get("name") == "opencode.generation.retry" and row.get("type") == "EVENT":
        return "retry"
    if row.get("type") == "TOOL":
        return "tool"
    return "other"


def percentile(values: list[float | int], p: float) -> float | int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[math.ceil(p * len(ordered)) - 1]


def latency_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = [
        value
        for row in rows
        if (value := scalar_number(row.get("latency"))) is not None
    ]
    return {
        "unit": "seconds",
        "reported": len(values),
        "missing": len(rows) - len(values),
        "p50": percentile(values, 0.5),
        "p95": percentile(values, 0.95),
    }


def usage_summary(
    rows: list[dict[str, Any]], keys: tuple[str, ...] = TOKEN_KEYS
) -> dict[str, Any]:
    result = {}
    for key in keys:
        values = [
            value for row in rows if (value := row["usageDetails"].get(key)) is not None
        ]
        result[key] = {
            "sum": sum(values) if values else None,
            "reported": len(values),
            "missing": len(rows) - len(values),
        }
    return result


def task_generation_usage(
    snapshot: dict[str, Any],
    turns: set[tuple[str, str]],
    attached: dict[tuple[str, str], list[dict[str, Any]]],
    cycle_affected: dict[tuple[str, str], bool],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    owners = task_evidence.binding_owners(snapshot)
    unique = {key for key, tasks in owners.items() if len(tasks) == 1}
    matched = unique & turns
    cyclic = {key for key in matched if cycle_affected.get(key, True)}
    eligible = matched - cyclic

    def generations(keys: set[tuple[str, str]]) -> list[dict[str, Any]]:
        return [
            row
            for key in keys
            for row in attached.get(key, [])
            if kind(row) == "generation"
        ]

    def metrics(keys: set[tuple[str, str]]) -> dict[str, Any]:
        rows = generations(keys)
        return {
            "eligible_turns": len(keys),
            "recorded_generation_observations": len(rows),
            "turns_without_observed_generations": sum(
                not generations({key}) for key in keys
            ),
            "reported_tokens": usage_summary(rows),
        }

    overall = {
        "evidence_class": "observed_usage_conditional_on_attested_binding",
        **metrics(eligible),
        "unmatched_unique_bindings": len(unique - turns),
        "cyclic_unique_bindings": len(cyclic),
        "ambiguous_bindings_excluded": len(owners) - len(unique),
        "limitations": [
            "Task-to-turn links are unverified attestations; exact ID matches do not prove task ownership.",
            "Only generations attached to eligible turns in the selected observation window are included; missing events and usage remain unknown.",
            "No child turns or tasks are rolled into parents unless separately bound; ambiguous and cyclic bindings are excluded.",
            "Token fields may overlap under provider conventions and must not be summed together blindly.",
        ],
    }
    per_task = {}
    for task in snapshot["tasks"]:
        keys = {
            (binding["trace_id"], binding["turn_id"]) for binding in task["bindings"]
        }
        selected = keys & eligible
        per_task[task["id"]] = {
            **metrics(selected),
            "unmatched_unique_bindings": len((keys & unique) - turns),
            "cyclic_unique_bindings": len(keys & cyclic),
            "ambiguous_bindings_excluded": len(keys - unique),
            "all_declared_bindings_eligible": bool(keys) and keys <= eligible,
        }
    return overall, per_task


def permission_wait_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Pair only one ask and one reply sharing the explicit request trace and session."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if (
            row["name"] == dashboard_spec.PERMISSION_ASK
            or row["name"] in dashboard_spec.PERMISSION_REPLIES
        ):
            groups[row["traceId"]].append(row)
    counts: Counter[str] = Counter()
    durations = []
    for group in groups.values():
        asks = [row for row in group if row["name"] == dashboard_spec.PERMISSION_ASK]
        replies = [
            row for row in group if row["name"] in dashboard_spec.PERMISSION_REPLIES
        ]
        if len(asks) > 1 or len(replies) > 1:
            counts["ambiguous_groups"] += 1
        elif not asks:
            counts["reply_without_ask"] += 1
        elif not replies:
            counts["ask_without_reply"] += 1
        elif not asks[0].get("sessionId") or asks[0].get("sessionId") != replies[0].get(
            "sessionId"
        ):
            counts["missing_or_mismatched_session"] += 1
        else:
            try:
                seconds = (
                    timestamp(replies[0].get("startTime"))
                    - timestamp(asks[0].get("startTime"))
                ).total_seconds()
            except ValueError:
                counts["invalid_timestamps"] += 1
                continue
            if seconds < 0:
                counts["reply_before_ask"] += 1
            else:
                counts["matched_pairs"] += 1
                durations.append(seconds)
    return {
        "ask_observations": sum(
            row["name"] == dashboard_spec.PERMISSION_ASK for row in rows
        ),
        "reply_observations": sum(
            row["name"] in dashboard_spec.PERMISSION_REPLIES for row in rows
        ),
        "request_groups": len(groups),
        **{
            key: counts[key]
            for key in (
                "matched_pairs",
                "ask_without_reply",
                "reply_without_ask",
                "ambiguous_groups",
                "missing_or_mismatched_session",
                "invalid_timestamps",
                "reply_before_ask",
            )
        },
        "matched_wait_seconds": {
            "count": len(durations),
            "p50": percentile(durations, 0.5),
            "p95": percentile(durations, 0.95),
        },
        "coverage": "unknown; missing events, time-window boundaries and uncaptured requests are not measured",
        "interpretation": "Elapsed time from observed ask to reply, not active work or task outcome.",
    }


def group_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "observations": len(rows),
        "marked_error": sum(row.get("level") == "ERROR" for row in rows),
        "level_missing": sum(row.get("level") is None for row in rows),
        "latency": latency_summary(rows),
    }


def safe_name(value: Any) -> str:
    return (
        value if isinstance(value, str) and value and len(value) <= 512 else "(unknown)"
    )


def by_name(
    rows: list[dict[str, Any]], field: str, *, include_usage: bool = False
) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[safe_name(row.get(field))].append(row)
    result = []
    for name, group in sorted(groups.items()):
        item = {"name": name, **group_summary(group)}
        if include_usage:
            item["reported_tokens"] = usage_summary(group)
        result.append(item)
    return result


def tool_sequence_candidates(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Name-only patterns are leads, not proof of equivalent inputs or mistakes."""
    tools = [row for row in rows if kind(row) == "tool"]
    try:
        ordered = sorted(
            tools, key=lambda row: (timestamp(row.get("startTime")), row["id"])
        )
    except ValueError:
        return {
            "available": False,
            "same_name_three_times": None,
            "alternating_pair_twice": None,
        }
    names = [safe_name(row.get("name")) for row in ordered]
    if "(unknown)" in names:
        return {
            "available": False,
            "same_name_three_times": None,
            "alternating_pair_twice": None,
        }
    return {
        "available": True,
        "same_name_three_times": any(
            names[i] == names[i + 1] == names[i + 2] for i in range(len(names) - 2)
        ),
        "alternating_pair_twice": any(
            names[i] != names[i + 1]
            and names[i] == names[i + 2]
            and names[i + 1] == names[i + 3]
            for i in range(len(names) - 3)
        ),
    }


def observed_time(
    turn: dict[str, Any], children: list[dict[str, Any]]
) -> dict[str, Any]:
    """Union clipped intervals; uncovered time is not an inferred wait."""
    try:
        start, end = timestamp(turn.get("startTime")), timestamp(turn.get("endTime"))
        if end < start:
            raise ValueError("Negative duration")
    except ValueError:
        return {"available": False, "reason": "invalid_turn_interval"}
    intervals = []
    invalid = 0
    clipped = 0
    for row in children:
        if kind(row) not in ("generation", "tool", "turn"):
            continue
        try:
            left, right = timestamp(row.get("startTime")), timestamp(row.get("endTime"))
            if right < left:
                raise ValueError("Negative duration")
        except ValueError:
            invalid += 1
            continue
        if left < start or right > end:
            clipped += 1
        left, right = max(start, left), min(end, right)
        if right > left:
            intervals.append((left, right))
    intervals.sort()
    total = 0.0
    current_start = current_end = None
    for left, right in intervals:
        if current_start is None:
            current_start, current_end = left, right
        elif left <= current_end:
            current_end = max(current_end, right)
        else:
            total += (current_end - current_start).total_seconds()
            current_start, current_end = left, right
    if current_start is not None:
        total += (current_end - current_start).total_seconds()
    duration = (end - start).total_seconds()
    return {
        "available": True,
        "turn_seconds": duration,
        "observed_child_union_seconds": total,
        "uncovered_by_observed_children_seconds": max(0.0, duration - total)
        if not invalid
        else None,
        "children_with_invalid_intervals": invalid,
        "children_outside_turn_interval": clipped,
    }


def build_report(
    rows: list[dict[str, Any]],
    start: dt.datetime,
    end: dt.datetime,
    duplicates: int = 0,
    include_identifiers: bool = False,
    session_scoped: bool = False,
    task_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    index = {(row["traceId"], row["id"]): row for row in rows}
    turns = [row for row in rows if kind(row) == "turn"]
    attached: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    unmatched = Counter()
    owner_cache: dict[tuple[str, str], tuple[str, str] | None] = {}

    # A parent cycle, including one that passes through a turn, invalidates ancestry.
    cycle_affected: dict[tuple[str, str], bool] = {}
    for key in index:
        path: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        current = key
        while (
            current in index and current not in cycle_affected and current not in seen
        ):
            seen.add(current)
            path.append(current)
            parent = index[current].get("parentObservationId")
            if not isinstance(parent, str) or not parent:
                current = (current[0], "")
                break
            current = (current[0], parent)
        affected = cycle_affected.get(current, current in seen)
        for item in path:
            cycle_affected[item] = affected

    def find_turn(row: dict[str, Any]) -> tuple[str, str] | None:
        trace = row["traceId"]
        current = (trace, row["id"])
        path: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        owner = None
        while True:
            if cycle_affected.get(current, False):
                break
            if current in owner_cache:
                owner = owner_cache[current]
                break
            if current in seen:
                break
            seen.add(current)
            ancestor = index.get(current)
            if ancestor is None:
                break
            if kind(ancestor) == "turn":
                owner = current
                break
            path.append(current)
            parent = ancestor.get("parentObservationId")
            if not isinstance(parent, str) or not parent:
                break
            current = (trace, parent)
        for key in path:
            owner_cache[key] = owner
        return owner

    for row in rows:
        category = kind(row)
        if category not in ("generation", "tool", "retry"):
            continue
        owner = find_turn(row)
        if owner is None:
            unmatched[category] += 1
        else:
            attached[owner].append(row)

    child_turns: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    turn_parents: dict[tuple[str, str], tuple[str, str]] = {}
    for turn in turns:
        if cycle_affected[(turn["traceId"], turn["id"])]:
            continue
        parent = turn.get("parentObservationId")
        if not isinstance(parent, str) or not parent or parent == turn["id"]:
            continue
        parent_row = index.get((turn["traceId"], parent))
        owner = find_turn(parent_row) if parent_row is not None else None
        if owner is not None and owner != (turn["traceId"], turn["id"]):
            turn_parents[(turn["traceId"], turn["id"])] = owner
    valid_turn_links: dict[tuple[str, str], bool] = {}
    for child in turn_parents:
        path: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        current = child
        while (
            current in turn_parents
            and current not in valid_turn_links
            and current not in seen
        ):
            seen.add(current)
            path.append(current)
            current = turn_parents[current]
        valid = valid_turn_links.get(current, current not in seen)
        for key in path:
            valid_turn_links[key] = valid
    for child, owner in turn_parents.items():
        if valid_turn_links[child]:
            child_turns[owner].append(index[child])
    linked_child_keys = {child for child, valid in valid_turn_links.items() if valid}

    linked = [row for group in attached.values() for row in group]
    tools = [row for row in linked if kind(row) == "tool"]
    generations = [row for row in linked if kind(row) == "generation"]
    retries = [row for row in linked if kind(row) == "retry"]
    runtime_events = [row for row in rows if kind(row) == "runtime"]
    compaction_events = [
        row
        for row in runtime_events
        if row["name"]
        in {*dashboard_spec.COMPACTION["ended"], *dashboard_spec.COMPACTION["failed"]}
    ]
    root_turns = [row for row in turns if row.get("isRootObservation") is True]
    patterns = {}
    timings = {}
    for turn in turns:
        key = (turn["traceId"], turn["id"])
        if cycle_affected[key]:
            patterns[key] = {
                "available": False,
                "same_name_three_times": None,
                "alternating_pair_twice": None,
            }
            timings[key] = {"available": False, "reason": "cyclic_ancestry"}
        else:
            patterns[key] = tool_sequence_candidates(attached[key])
            timings[key] = observed_time(turn, [*attached[key], *child_turns[key]])
    daily: dict[str, Counter] = defaultdict(Counter)
    weekly: dict[str, Counter] = defaultdict(Counter)
    missing_start = 0
    for row in [*turns, *linked, *runtime_events]:
        try:
            date = timestamp(row.get("startTime")).date()
        except ValueError:
            missing_start += 1
            continue
        year, week, _ = date.isocalendar()
        for bucket in (daily[date.isoformat()], weekly[f"{year}-W{week:02d}"]):
            bucket[kind(row)] += 1
            if kind(row) == "generation":
                total = row["usageDetails"].get("total")
                if total is not None:
                    bucket["reported_total_tokens"] += total
                    bucket["generations_with_total_tokens"] += 1

    report: dict[str, Any] = {
        "schema_version": "1.0",
        "source": "langfuse-v2-observations",
        "expected_langfuse_version": "4.82.0",
        "computed_at": dt.datetime.now(UTC).isoformat(),
        "window": {
            "from_inclusive": start.isoformat(),
            "to_exclusive": end.isoformat(),
            "timezone": "UTC",
            "basis": "observation start time",
            "scope": "one session" if session_scoped else "project",
        },
        "coverage": {
            "observations_scanned": len(rows),
            "exact_duplicates_ignored": duplicates,
            "turns": len(turns),
            "root_turns": len(root_turns),
            "linked_child_turns": sum(len(group) for group in child_turns.values()),
            "nonroot_turns_without_link": sum(
                turn.get("isRootObservation") is False
                and (turn["traceId"], turn["id"]) not in linked_child_keys
                for turn in turns
            ),
            "ambiguous_child_turn_links": sum(
                not valid for valid in valid_turn_links.values()
            )
            + sum(cycle_affected[(turn["traceId"], turn["id"])] for turn in turns),
            "cycle_affected_observations": sum(cycle_affected.values()),
            "turns_with_unknown_root_status": sum(
                not isinstance(row.get("isRootObservation"), bool) for row in turns
            ),
            "attached_generations": len(generations),
            "unattached_generation_candidates": unmatched["generation"],
            "attached_tools": len(tools),
            "unattached_project_tools": unmatched["tool"],
            "attached_retries": len(retries),
            "unattached_retry_candidates": unmatched["retry"],
            "missing_or_invalid_start_time": missing_start,
            "limitations": [
                "Only observations returned in the time window can be linked; parents or children outside it are missing.",
                "Unattached project tools can belong to other integrations, incomplete traces or the window boundary.",
                "Unexported, in-flight and dropped observations are not counted.",
                "A single-session filter excludes other sessions, including child sessions.",
            ],
        },
        "root_turns": group_summary(root_turns),
        "all_turns": group_summary(turns),
        "linked_generations": {
            **group_summary(generations),
            "reported_tokens": usage_summary(generations),
            "by_model": by_name(generations, "model", include_usage=True),
        },
        "linked_tools": {**group_summary(tools), "by_name": by_name(tools, "name")},
        "linked_retry_events": len(retries),
        "runtime_events": {
            "observations": len(runtime_events),
            "by_name": dict(
                sorted(Counter(row["name"] for row in runtime_events).items())
            ),
            "compaction_terminal_observations": len(compaction_events),
            "compaction_reported_tokens": usage_summary(
                compaction_events,
                ("input", "output", "reasoning", "cache_read", "cache_write"),
            ),
            "permission_waits": permission_wait_summary(runtime_events),
            "coverage": "unknown; opt-in live capture may be disabled, interrupted or lose events",
            "scope": "session busy periods, compaction and permission requests; not prompt or task outcomes",
        },
        "tool_sequence_candidates": {
            "description": "Name-only sequences ordered by start time; inputs, causality, concurrency and correctness are not established.",
            "turns_evaluated": sum(item["available"] for item in patterns.values()),
            "turns_unavailable": sum(
                not item["available"] for item in patterns.values()
            ),
            "same_name_three_times": sum(
                item["same_name_three_times"] is True for item in patterns.values()
            ),
            "alternating_pair_twice": sum(
                item["alternating_pair_twice"] is True for item in patterns.values()
            ),
        },
        "turn_timing_coverage": {
            "turns_without_usable_timing": sum(
                not item["available"] for item in timings.values()
            ),
            "turns_with_invalid_child_intervals": sum(
                item.get("children_with_invalid_intervals", 0) > 0
                for item in timings.values()
            ),
            "interpretation": "Uncovered time excludes observed linked child intervals only; it is not proof of a human, tool, service or unnecessary wait.",
        },
        "by_day_utc": [
            {"day": day, **dict(counts)} for day, counts in sorted(daily.items())
        ],
        "by_iso_week_utc": [
            {"week": week, **dict(counts)} for week, counts in sorted(weekly.items())
        ],
        "unknown": {
            "task_completion": "Turn end and non-error level do not establish task success.",
            "task_epic_links": "Explicit task hierarchy is not available in these observations.",
            "satisfaction_and_rework": "Human feedback and verified task outcomes are not available.",
            "blockers_and_root_cause": "Error levels alone do not establish blockers or cause.",
            "context_and_compaction_quality": "Context capacity and compaction quality are not available here.",
        },
    }
    if task_snapshot is not None:
        report["task_attestations"] = task_evidence.summarize(
            task_snapshot,
            {(turn["traceId"], turn["id"]) for turn in turns},
            include_identifiers,
        )
        usage, per_task_usage = task_generation_usage(
            task_snapshot,
            {(turn["traceId"], turn["id"]) for turn in turns},
            attached,
            cycle_affected,
        )
        report["task_attestations"]["conditional_generation_usage"] = usage
        if include_identifiers:
            for task in report["task_attestations"]["tasks"]:
                task["conditional_generation_usage"] = per_task_usage[task["id"]]
    if include_identifiers:
        details = []
        sessions: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in [*turns, *linked, *runtime_events]:
            sessions[safe_name(row.get("sessionId"))].append(row)
        for turn in sorted(
            turns, key=lambda item: (item.get("startTime") or "", item["id"])
        ):
            children = attached[(turn["traceId"], turn["id"])]
            timeline = sorted(
                [*children, *child_turns[(turn["traceId"], turn["id"])]],
                key=lambda item: (item.get("startTime") or "", item["id"]),
            )
            details.append(
                {
                    "trace_id": turn["traceId"],
                    "observation_id": turn["id"],
                    "session_id": turn.get("sessionId"),
                    "start_time": turn.get("startTime"),
                    "end_time": turn.get("endTime"),
                    "logical_root": turn.get("isRootObservation"),
                    "level": turn.get("level"),
                    "tool_sequence_candidates": patterns[(turn["traceId"], turn["id"])],
                    "observed_timing": timings[(turn["traceId"], turn["id"])],
                    "timeline": [
                        {
                            "observation_id": row["id"],
                            "kind": kind(row),
                            "name": safe_name(row.get("name")),
                            "start_time": row.get("startTime"),
                            "end_time": row.get("endTime"),
                            "latency_seconds": scalar_number(row.get("latency")),
                            "level": row.get("level"),
                        }
                        for row in timeline
                    ],
                    "reported_tokens": usage_summary(
                        [row for row in children if kind(row) == "generation"]
                    ),
                }
            )
        report["turn_details"] = details
        report["sessions"] = [
            {
                "session_id": session,
                "turns": sum(kind(row) == "turn" for row in group),
                "linked_tools": sum(kind(row) == "tool" for row in group),
                "linked_generations": sum(kind(row) == "generation" for row in group),
                "runtime_events": dict(
                    sorted(
                        Counter(
                            row["name"] for row in group if kind(row) == "runtime"
                        ).items()
                    )
                ),
                "reported_tokens": usage_summary(
                    [row for row in group if kind(row) == "generation"]
                ),
            }
            for session, group in sorted(sessions.items())
        ]
    return report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--from-time",
        help="Inclusive ISO 8601 start with timezone (default: seven days ago)",
    )
    parser.add_argument(
        "--to-time", help="Exclusive ISO 8601 end with timezone (default: now)"
    )
    parser.add_argument(
        "--max-observations",
        type=int,
        default=10000,
        help="Fail rather than truncate after this many observations (1–100000)",
    )
    parser.add_argument(
        "--include-identifiers",
        action="store_true",
        help="Include private trace/session IDs and structural per-turn timelines",
    )
    parser.add_argument(
        "--session-id",
        help="Restrict the scan to one session; child sessions are not included",
    )
    parser.add_argument(
        "--task-evidence",
        type=Path,
        help="Read an explicitly selected private JSON task-attestation snapshot",
    )
    args = parser.parse_args(argv)
    end = timestamp(args.to_time) if args.to_time else dt.datetime.now(UTC)
    start = timestamp(args.from_time) if args.from_time else end - dt.timedelta(days=7)
    if start >= end or end - start > dt.timedelta(days=90):
        raise ValueError("The time window must be positive and at most 90 days.")
    if not 1 <= args.max_observations <= 100000:
        raise ValueError("The observation limit must be between 1 and 100000.")
    snapshot = (
        task_evidence.read_file(args.task_evidence) if args.task_evidence else None
    )
    # The credential helper may print a non-secret diagnostic; keep stdout JSON-only.
    with contextlib.redirect_stdout(sys.stderr):
        client, _, _ = span_dashboard.context()
    rows, duplicates = fetch_observations(
        client, start, end, args.max_observations, args.session_id
    )
    print(
        json.dumps(
            build_report(
                rows,
                start,
                end,
                duplicates,
                args.include_identifiers,
                session_scoped=args.session_id is not None,
                task_snapshot=snapshot,
            ),
            indent=2,
            allow_nan=False,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError) as exc:
        print(f"Agent report failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
