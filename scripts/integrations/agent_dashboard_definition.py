"""Code-owned native dashboards for recorded OpenCode and project observations."""

from __future__ import annotations

from typing import Any


def options(column: str, *values: str) -> dict[str, Any]:
    return {
        "column": column,
        "type": "stringOptions",
        "operator": "any of",
        "value": list(values),
    }


TURN = [
    options("name", "opencode.turn"),
    options("type", "AGENT"),
    {"column": "isRootObservation", "type": "boolean", "operator": "=", "value": True},
]
GENERATION = [
    options("name", "opencode.generation", "opencode.generation.failed"),
    options("type", "GENERATION"),
]
TOOL = [options("type", "TOOL")]
ERROR = options("level", "ERROR")


def widget(
    name: str,
    description: str,
    filters: list[dict[str, Any]],
    measure: str = "count",
    agg: str = "count",
    dimension: str | None = None,
    chart: str = "NUMBER",
) -> dict[str, Any]:
    config: dict[str, Any] = {"type": chart}
    if chart == "HORIZONTAL_BAR":
        config["row_limit"] = 20
    return {
        "name": name,
        "description": description,
        "view": "observations",
        "dimensions": [{"field": dimension}] if dimension else [],
        "metrics": [{"measure": measure, "agg": agg}],
        "filters": filters,
        "chartType": chart,
        "chartConfig": config,
    }


def definitions() -> dict[str, dict[str, Any]]:
    """Names, measures and caveats are deliberately editable in code."""
    return {
        "operations": {
            "name": "AI Observatory — OpenCode operations",
            "description": (
                "Recorded OpenCode root turns and model observations. An ended or non-error turn "
                "is not proof of task success. Missing or unexported data is not shown."
            ),
            "widgets": {
                "turns": widget(
                    "Root prompt turns",
                    "Recorded logical-root OpenCode turns; not unique tasks.",
                    TURN,
                ),
                "turn_errors": widget(
                    "Root turns marked error",
                    "Explicit ERROR level only; non-error does not mean success.",
                    [*TURN, ERROR],
                ),
                "turn_p50": widget(
                    "Root turn duration p50",
                    "Elapsed turn duration; includes any waiting.",
                    TURN,
                    "latency",
                    "p50",
                ),
                "turn_p95": widget(
                    "Root turn duration p95",
                    "Elapsed turn duration; interpret with root turn count.",
                    TURN,
                    "latency",
                    "p95",
                ),
                "turn_trend": widget(
                    "Root turns over time",
                    "Recorded turns by start time, not completed tasks.",
                    TURN,
                    chart="BAR_TIME_SERIES",
                ),
                "generations": widget(
                    "OpenCode model observations",
                    "Model steps, including child sessions; not requests or tasks.",
                    GENERATION,
                ),
                "generation_errors": widget(
                    "Model observations marked error",
                    "Explicit generation errors, including recovered attempts.",
                    [*GENERATION, ERROR],
                ),
                "retries": widget(
                    "Recorded model retries",
                    "Retry events exported by the OpenCode plugin; missing events are unknown.",
                    [
                        options("name", "opencode.generation.retry"),
                        options("type", "EVENT"),
                    ],
                ),
                "generation_trend": widget(
                    "Model observations over time",
                    "OpenCode generation observations by start time.",
                    GENERATION,
                    chart="BAR_TIME_SERIES",
                ),
            },
        },
        "performance": {
            "name": "AI Observatory — Models and project tools",
            "description": (
                "OpenCode model observations plus project-wide TOOL observations. Tools can come "
                "from other integrations; counts and elapsed duration are not measures of correctness."
            ),
            "widgets": {
                "model_calls": widget(
                    "OpenCode model observations by model",
                    "Generation count by provided model; names may be missing.",
                    GENERATION,
                    dimension="providedModelName",
                    chart="HORIZONTAL_BAR",
                ),
                "model_p95": widget(
                    "OpenCode model duration p95 by model",
                    "Generation elapsed duration; compare with model observation counts.",
                    GENERATION,
                    "latency",
                    "p95",
                    "providedModelName",
                    "HORIZONTAL_BAR",
                ),
                "model_tokens": widget(
                    "OpenCode reported tokens by model",
                    "Sum of reported generation totals; availability and provider semantics vary.",
                    GENERATION,
                    "totalTokens",
                    "sum",
                    "providedModelName",
                    "HORIZONTAL_BAR",
                ),
                "tokens_trend": widget(
                    "OpenCode reported tokens over time",
                    "Reported generation total tokens; missing usage is not a measured zero.",
                    GENERATION,
                    "totalTokens",
                    "sum",
                    chart="BAR_TIME_SERIES",
                ),
                "model_cost": widget(
                    "OpenCode reported cost by model",
                    "Recorded/inferred provider cost only, not a guaranteed bill; cost may be missing.",
                    GENERATION,
                    "totalCost",
                    "sum",
                    "providedModelName",
                    "HORIZONTAL_BAR",
                ),
                "tools": widget(
                    "Project-wide tool observations",
                    "All TOOL observations in this Langfuse project, not only OpenCode.",
                    TOOL,
                ),
                "tool_names": widget(
                    "Project-wide tool calls by name",
                    "All project TOOL observations, including other integrations.",
                    TOOL,
                    dimension="name",
                    chart="HORIZONTAL_BAR",
                ),
                "tool_errors": widget(
                    "Project-wide tools marked error",
                    "Explicit TOOL error levels only; tool-level failures may be unmarked.",
                    [*TOOL, ERROR],
                    dimension="name",
                    chart="HORIZONTAL_BAR",
                ),
                "tool_p50": widget(
                    "Project-wide tool duration p50",
                    "Elapsed TOOL duration by name; includes other integrations.",
                    TOOL,
                    "latency",
                    "p50",
                    "name",
                    "HORIZONTAL_BAR",
                ),
                "tool_p95": widget(
                    "Project-wide tool duration p95",
                    "Elapsed TOOL duration by name; compare with per-tool counts.",
                    TOOL,
                    "latency",
                    "p95",
                    "name",
                    "HORIZONTAL_BAR",
                ),
                "tool_trend": widget(
                    "Project-wide tools over time",
                    "TOOL observations across the Langfuse project, not only OpenCode.",
                    TOOL,
                    chart="BAR_TIME_SERIES",
                ),
            },
        },
    }
