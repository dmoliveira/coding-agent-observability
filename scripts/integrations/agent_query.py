"""Bounded private query pages for observed tool and model activity.

This is an in-process adapter, not an HTTP service or browser viewer. The
upstream cursor stays on the query object and is never serialized to callers.
"""

from __future__ import annotations

import datetime as dt
import json
import time
import urllib.parse
from typing import Any

import common

from integrations import agent_report, span_dashboard

UTC = dt.timezone.utc
OBSERVATIONS_PATH = "/api/public/v2/observations"
DEFAULT_PAGE_ROWS = 100
MAX_PAGE_ROWS = 100
MAX_QUERY_PAGES = 1000
MAX_QUERY_ROWS = 100_000
MAX_QUERY_SECONDS = 120
MAX_QUERY_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_PAGE_OUTPUT_BYTES = 1024 * 1024
MAX_SEARCH_BYTES = 256
MAX_CURSOR_BYTES = 4096

QUERY_FIELDS = "basic,model,usage,metrics"
QUERY_KINDS = {
    "tool": ("TOOL", "name"),
    "model": ("GENERATION", "model"),
}


def _window(start: dt.datetime, end: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    if (
        not isinstance(start, dt.datetime)
        or start.tzinfo is None
        or start.utcoffset() is None
        or not isinstance(end, dt.datetime)
        or end.tzinfo is None
        or end.utcoffset() is None
    ):
        raise ValueError("Query times must be timezone-aware datetimes.")
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    if start >= end or end - start > dt.timedelta(days=90):
        raise ValueError("The query window must be positive and at most 90 days.")
    return start, end


def _search_term(search: str) -> str:
    if not isinstance(search, str):
        raise TypeError("Search must be a string.")
    try:
        size = len(search.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError("Search must be valid UTF-8 text.") from None
    if size > MAX_SEARCH_BYTES or any(
        ord(char) < 32 or ord(char) == 127 for char in search
    ):
        raise ValueError("Search must be at most 256 bytes without control characters.")
    return search


def _integer_limit(value: int, *, maximum: int, label: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise ValueError(f"{label} must be between 1 and {maximum}.")
    return value


def _page_observation(
    row: dict[str, Any], *, include_identifiers: bool
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "type": row["type"],
        "name": row["name"],
        "model": row["model"],
        "start_time": row["startTime"],
        "end_time": row["endTime"],
        "level": row["level"],
        "latency_seconds": row["latency"],
    }
    if row["type"] == "GENERATION":
        item["usage_details"] = row["usageDetails"]
    if include_identifiers:
        item["identifiers"] = {
            "observation_id": row["id"],
            "trace_id": row["traceId"],
            "parent_observation_id": row["parentObservationId"],
            "session_id": row["sessionId"],
        }
    return item


class ObservationQuery:
    """Cursor through a bounded search of observed tools or generations.

    Each `next_page_json` call returns exactly one serialized page. Budget and
    cursor state stay on this object for the lifetime of the query; callers
    must create a new query rather than retry a failed or exhausted scan.
    """

    def __init__(
        self,
        client: span_dashboard.Client,
        start: dt.datetime,
        end: dt.datetime,
        *,
        kind: str,
        search: str = "",
        page_size: int = DEFAULT_PAGE_ROWS,
        include_identifiers: bool = False,
        max_pages: int = MAX_QUERY_PAGES,
        max_rows: int = MAX_QUERY_ROWS,
        max_page_output_bytes: int = MAX_PAGE_OUTPUT_BYTES,
        budget: agent_report.ReportBudget | None = None,
        clock: Any = time.monotonic,
    ) -> None:
        if kind not in QUERY_KINDS:
            raise ValueError("Query kind must be 'tool' or 'model'.")
        if not isinstance(include_identifiers, bool):
            raise TypeError("Identifier selection must be boolean.")
        self.client = client
        self.start, self.end = _window(start, end)
        self.kind = kind
        self.expected_type, self.search_field = QUERY_KINDS[kind]
        self.search = _search_term(search)
        self.page_size = _integer_limit(
            page_size, maximum=MAX_PAGE_ROWS, label="Page size"
        )
        self.max_pages = _integer_limit(
            max_pages, maximum=MAX_QUERY_PAGES, label="Query page budget"
        )
        self.max_rows = _integer_limit(
            max_rows, maximum=MAX_QUERY_ROWS, label="Query row budget"
        )
        self.max_page_output_bytes = _integer_limit(
            max_page_output_bytes,
            maximum=MAX_PAGE_OUTPUT_BYTES,
            label="Page output-byte budget",
        )
        self.include_identifiers = include_identifiers
        self.budget = budget or agent_report.ReportBudget(
            max_seconds=MAX_QUERY_SECONDS,
            max_response_bytes=MAX_QUERY_RESPONSE_BYTES,
            clock=clock,
        )
        if (
            isinstance(self.budget.max_response_bytes, bool)
            or not isinstance(self.budget.max_response_bytes, int)
            or not 1 <= self.budget.max_response_bytes <= MAX_QUERY_RESPONSE_BYTES
        ):
            raise ValueError("Query response-byte budget must be positive and bounded.")

        self._cursor: str | None = None
        self._seen_cursors: set[str] = set()
        self._seen_observations: set[tuple[str, str]] = set()
        self._pages = 0
        self._rows = 0
        self._last_page_output_bytes = 0
        self._last_response_bytes = getattr(client, "response_bytes_received", None)
        self._complete = False
        self._failed = False
        remaining_seconds = self.budget.deadline - self.budget.clock()
        if remaining_seconds > MAX_QUERY_SECONDS:
            raise ValueError("Query wall-time budget exceeds the declared maximum.")
        self._max_wall_seconds = max(0.0, remaining_seconds)

    def _params(self) -> dict[str, Any]:
        params: dict[str, Any] = {
            "fields": QUERY_FIELDS,
            "limit": self.page_size,
            "fromStartTime": self.start.isoformat(),
            "toStartTime": self.end.isoformat(),
            "type": self.expected_type,
        }
        if self.search:
            params["filter"] = json.dumps(
                [
                    {
                        "type": "string",
                        "column": self.search_field,
                        "operator": "contains",
                        "value": self.search,
                    }
                ],
                separators=(",", ":"),
            )
        if self._cursor is not None:
            params["cursor"] = self._cursor
        return params

    def _response_bytes(self, page: dict[str, Any]) -> int:
        current = getattr(self.client, "response_bytes_received", None)
        if isinstance(self._last_response_bytes, int) and isinstance(current, int):
            result = current - self._last_response_bytes
            self._last_response_bytes = current
            return result
        return len(json.dumps(page, separators=(",", ":")).encode("utf-8"))

    def _envelope(
        self,
        rows: list[dict[str, Any]],
        *,
        complete: bool,
        page_output_bytes: int,
    ) -> dict[str, Any]:
        return {
            "schema_version": "aiobs.observation-query.v1",
            "source": "langfuse-v2-observations",
            "compatibility_target": "Langfuse 4.82.0 public v2 observations API",
            "evidence_class": "observed",
            "computed_at": dt.datetime.now(UTC).isoformat(),
            "query": {
                "kind": self.kind,
                "search_field": self.search_field,
                "search_mode": (
                    "Langfuse string contains filter" if self.search else "none"
                ),
                "window": {
                    "from_inclusive": self.start.isoformat(),
                    "to_exclusive": self.end.isoformat(),
                    "timezone": "UTC",
                    "scope": "Langfuse project observations",
                },
                "ordering": "Langfuse cursor order; not categorical or alphabetical",
            },
            "page": {
                "number": self._pages,
                "limit": self.page_size,
                "returned": len(rows),
                "rows_seen": self._rows + len(rows),
                "continuation_available": not complete,
                "query_complete": complete,
                "total_matches": None,
                "total_matches_reason": "The cursor API does not provide a total count.",
            },
            "budget": {
                "declared": {
                    "max_pages": self.max_pages,
                    "max_rows": self.max_rows,
                    "max_rows_per_page": self.page_size,
                    "max_response_bytes": self.budget.max_response_bytes,
                    "max_response_bytes_per_page": span_dashboard.MAX_RESPONSE,
                    "max_output_bytes_per_page": self.max_page_output_bytes,
                    "max_request_seconds": 30,
                    "max_wall_seconds": self._max_wall_seconds,
                    "max_concurrent_query_requests_per_checkout": 1,
                },
                "observed": {
                    "pages": self._pages,
                    "rows": self._rows + len(rows),
                    "response_bytes": self.budget.response_bytes,
                    "page_output_bytes": page_output_bytes,
                },
            },
            "observations": rows,
            "coverage_notes": [
                "Pagination reached the current end only when query_complete is true; delayed, in-flight, dropped or unexported observations may be absent.",
                "Tool rows are project-wide TOOL observations; they are not necessarily emitted by OpenCode.",
                "Generation rows are observed model calls, not a complete available-model catalog.",
                "Missing usage fields are null; reported zero remains zero.",
            ],
        }

    def next_page_json(self) -> str:
        """Fetch and serialize one page, failing rather than emitting a false empty."""
        try:
            with common.report_query_lock():
                return self._next_page_json_locked()
        except common.ReportQueryBusy:
            self._failed = True
            raise agent_report.BudgetExceeded(
                "Viewer query concurrency budget exhausted; retry with a new query after the active report finishes."
            ) from None

    def _next_page_json_locked(self) -> str:
        if self._complete:
            raise ValueError("The observation query is already complete.")
        if self._failed:
            raise ValueError(
                "The observation query is incomplete and cannot be resumed."
            )
        if self._pages >= self.max_pages:
            self._failed = True
            raise agent_report.BudgetExceeded(
                "Viewer query page budget exhausted; narrow the window or search."
            )
        if self._rows >= self.max_rows:
            self._failed = True
            raise agent_report.BudgetExceeded(
                "Viewer query row budget exhausted; narrow the window or search."
            )

        attempted = False
        admitted = False
        try:
            timeout = self.budget.admit_request()
            admitted = True
            attempted = True
            self._pages += 1
            url = OBSERVATIONS_PATH + "?" + urllib.parse.urlencode(self._params())
            response_limit = self.budget.response_limit()
            with agent_report.enforce_wall_time_budget(self.budget):
                try:
                    page = self.client.request(
                        "GET",
                        url,
                        timeout=timeout,
                        max_response_bytes=response_limit,
                    )
                except span_dashboard.ResponseBudgetExceeded:
                    raise agent_report.BudgetExceeded(
                        "Viewer query response-byte budget exceeded; narrow the window."
                    ) from None
            self.budget.check()
            if not isinstance(page, dict):
                raise TypeError("Langfuse returned invalid observation pagination.")
            response_bytes = self._response_bytes(page)
            if response_bytes > response_limit:
                raise agent_report.BudgetExceeded(
                    "Viewer query response-byte budget exceeded; narrow the window."
                )
            self.budget.record_response_bytes(response_bytes)

            raw_rows, meta = page.get("data"), page.get("meta")
            if not isinstance(raw_rows, list) or not isinstance(meta, dict):
                raise TypeError("Langfuse returned invalid observation pagination.")
            if len(raw_rows) > self.page_size:
                raise agent_report.BudgetExceeded(
                    "Viewer query page-row budget exceeded; reduce the page size."
                )

            selected_rows = []
            page_keys: set[tuple[str, str]] = set()
            for raw_row in raw_rows:
                row = agent_report.select(raw_row)
                if row["type"] != self.expected_type:
                    raise ValueError(
                        "Langfuse returned a row outside the requested type."
                    )
                key = (row["traceId"], row["id"])
                if key in self._seen_observations or key in page_keys:
                    raise ValueError(
                        "Langfuse repeated an observation across query pages."
                    )
                page_keys.add(key)
                selected_rows.append(
                    _page_observation(row, include_identifiers=self.include_identifiers)
                )

            if self._rows + len(selected_rows) > self.max_rows:
                raise agent_report.BudgetExceeded(
                    "Viewer query row budget exhausted; narrow the window or search."
                )

            cursor = meta.get("cursor")
            if cursor is not None:
                try:
                    cursor_size = len(cursor.encode("utf-8"))
                except (AttributeError, UnicodeEncodeError):
                    cursor_size = MAX_CURSOR_BYTES + 1
                if (
                    not isinstance(cursor, str)
                    or not cursor
                    or cursor_size > MAX_CURSOR_BYTES
                    or cursor in self._seen_cursors
                ):
                    raise ValueError(
                        "Langfuse returned an invalid or repeated query cursor."
                    )
            if cursor is not None and self._pages >= self.max_pages:
                raise agent_report.BudgetExceeded(
                    "Viewer query page budget exhausted before the last page; narrow the window or search."
                )
            if cursor is not None and self._rows + len(selected_rows) >= self.max_rows:
                raise agent_report.BudgetExceeded(
                    "Viewer query row budget exhausted before the last page; narrow the window or search."
                )

            complete = cursor is None
            output_bytes = 0
            for _ in range(4):
                envelope = self._envelope(
                    selected_rows,
                    complete=complete,
                    page_output_bytes=output_bytes,
                )
                output = agent_report.serialize_report(
                    envelope,
                    maximum=self.max_page_output_bytes,
                    budget=self.budget,
                )
                measured_bytes = len(output.encode("utf-8"))
                if measured_bytes == output_bytes:
                    break
                output_bytes = measured_bytes
            else:
                raise agent_report.BudgetExceeded(
                    "Viewer query page output-byte budget could not be accounted safely."
                )

            self._seen_observations.update(page_keys)
            self._rows += len(selected_rows)
            self._last_page_output_bytes = output_bytes
            if cursor is None:
                self._complete = True
            else:
                self._seen_cursors.add(cursor)
                self._cursor = cursor
            return output
        except BaseException as exc:
            if attempted or isinstance(exc, agent_report.BudgetExceeded):
                self._failed = True
            raise
        finally:
            if admitted:
                self.budget.release_request()
