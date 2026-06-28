"""Dynatrace (read-only performance trends / problems) MCP tool registrations.

Each tool's docstring starts with "Dynatrace:" so the orchestrator's LLM can
route historical-metric intents away from MQ ("IBM MQ:"), ACE ("IBM ACE:"),
certificate ("Certificate:") and Splunk ("Splunk:") tools. Tool names are
prefixed with `dynatrace_`.

All tools are read-only: only Metrics / Entities / Problems API v2 GET endpoints
are called, and the Dynatrace host is checked against the allow-list before any
request. These answer the historical "trend / statistics over time" questions —
server CPU/memory/disk, MQ/ACE component metrics, and problem history — that the
live `mq_*`/`ace_*` tools and the Splunk log search cannot. The orchestrator can
pair a Dynatrace trend (or open problem) with a live inspection for root-cause.
"""
from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from server.config import (
    DYNATRACE_ACE_METRIC_SELECTORS,
    DYNATRACE_HOST_METRIC_SELECTORS,
    DYNATRACE_MQ_METRIC_SELECTORS,
)
from server.dynatrace_helpers import (
    _dt_quote,
    list_metrics,
    metric_stats,
    run_metric_query,
    run_problems,
)
from server.logger import get_logger
from server.query_log import logged_tool

logger = get_logger("mqacemcpserver.dynatrace.tools")


def _as_str_list(value) -> list[str]:
    """Normalise a list/str argument to a clean, de-duplicated list of strings."""
    if value is None:
        return []
    items = [value] if isinstance(value, str) else list(value)
    cleaned = [str(v).strip() for v in items if v is not None and str(v).strip()]
    return list(dict.fromkeys(cleaned))


def _err(message: str, **extra) -> str:
    return json.dumps({"status": "error", "message": message, **extra}, indent=2)


async def _metrics_envelope(
    entity_label: str,
    names: list[str],
    entity_type: str,
    metric_selectors: list[str],
    frm: str,
    to: str,
) -> str:
    """Query each metric selector scoped to the named entities; summarise.

    `entity_type` is a Dynatrace entity type (e.g. HOST, PROCESS_GROUP_INSTANCE).
    Entities are matched by name via `entityName.in(...)`; user names are quoted
    through `_dt_quote` (selector-injection safe).
    """
    quoted = ",".join(_dt_quote(n) for n in names)
    entity_selector = f'type("{entity_type}"),entityName.in({quoted})'

    series: list[dict] = []
    errors: list[str] = []
    for selector in metric_selectors:
        results, error = await run_metric_query(
            selector, entity_selector=entity_selector, frm=frm, to=to
        )
        if error is not None:
            errors.append(error)
            continue
        for result in results:
            series.extend(metric_stats(result))

    if not series and errors:
        return _err(errors[0], **{entity_label: names})

    return json.dumps(
        {
            "status": "success",
            entity_label: names,
            "entity_type": entity_type,
            "metric_selectors": metric_selectors,
            "from": frm,
            "to": to,
            "count": len(series),
            "message": (
                f"{len(series)} metric series found."
                if series
                else "No matching metric data in the time window."
            ),
            "series": series,
            "partial_errors": errors or None,
        },
        indent=2,
        default=str,
    )


def register(mcp: FastMCP) -> None:
    """Attach every Dynatrace tool to the given FastMCP instance."""

    @mcp.tool()
    @logged_tool
    async def dynatrace_host_performance(
        hostnames: list[str],
        metrics: list[str] | None = None,
        frm: str = "now-24h",
        to: str = "now",
    ) -> str:
        """Dynatrace: Server performance trend + stats (CPU, memory, disk) for one or more hosts.

        Returns avg/min/max/last over the time window for each host and metric.
        Answers "CPU/memory/disk trend on lodmq01 over the last 24h", "is the MQ
        server under memory pressure", "disk usage on lotace03 this week".

        Pass MULTIPLE hosts to cover them in one call — e.g.
        `hostnames=["lodmq01", "lodmq02"]`.

        Args:
            hostnames: One or more host names (matched against Dynatrace HOST
                entity names).
            metrics: Optional explicit metric selectors. OMIT to use the
                configured host defaults (CPU usage, memory usage, disk used %,
                CPU load). Pass exact `builtin:host.*` keys only when the user
                names a specific metric.
            frm: Dynatrace timeframe start (default "now-24h"). Examples:
                "now-1h", "now-7d".
            to: Dynatrace timeframe end (default "now").
        """
        hosts = _as_str_list(hostnames)
        if not hosts:
            return _err('No host supplied. Pass hostnames=["lodmq01"].')
        selectors = _as_str_list(metrics) or DYNATRACE_HOST_METRIC_SELECTORS
        if not selectors:
            return _err(
                "No host metric selectors configured. Set "
                "DYNATRACE_HOST_METRIC_SELECTORS or pass metrics=[...]."
            )
        return await _metrics_envelope(
            "hostnames", hosts, "HOST", selectors, frm, to
        )

    @mcp.tool()
    @logged_tool
    async def dynatrace_mq_metrics(
        qmgr_names: list[str],
        metrics: list[str] | None = None,
        frm: str = "now-24h",
        to: str = "now",
    ) -> str:
        """Dynatrace: Historical IBM MQ component metrics/trends for one or more queue managers.

        Returns avg/min/max/last over the window for MQ component metrics (e.g.
        queue depth, message rates, channel status) captured by the Dynatrace IBM
        MQ extension. Answers "queue depth trend for QM1 today", "message rate on
        QM1 this week".

        The MQ metric keys are deployment-specific (they depend on the installed
        extension). If none are configured, run `dynatrace_list_metrics` to find
        them and set DYNATRACE_MQ_METRIC_SELECTORS (or pass `metrics=[...]`).

        Args:
            qmgr_names: One or more queue manager names (matched against the MQ
                process-group entity names).
            metrics: Optional explicit metric selectors (overrides the configured
                MQ defaults).
            frm: Dynatrace timeframe start (default "now-24h").
            to: Dynatrace timeframe end (default "now").
        """
        qms = _as_str_list(qmgr_names)
        if not qms:
            return _err('No queue manager supplied. Pass qmgr_names=["QM1"].')
        selectors = _as_str_list(metrics) or DYNATRACE_MQ_METRIC_SELECTORS
        if not selectors:
            return _err(
                "No IBM MQ metric selectors configured. These are "
                "deployment-specific — run dynatrace_list_metrics (e.g. "
                'search_strings=["mq"]) to discover the keys, then set '
                "DYNATRACE_MQ_METRIC_SELECTORS or pass metrics=[...]."
            )
        return await _metrics_envelope(
            "qmgr_names", qms, "PROCESS_GROUP_INSTANCE", selectors, frm, to
        )

    @mcp.tool()
    @logged_tool
    async def dynatrace_ace_metrics(
        nodes: list[str],
        metrics: list[str] | None = None,
        frm: str = "now-24h",
        to: str = "now",
    ) -> str:
        """Dynatrace: Historical IBM ACE component metrics/trends for one or more integration nodes.

        Returns avg/min/max/last over the window for ACE component metrics (e.g.
        message-flow throughput, processing time) captured by the Dynatrace ACE
        extension. Answers "flow throughput trend on NODE1 today", "ACE
        processing time this week".

        The ACE metric keys are deployment-specific. If none are configured, run
        `dynatrace_list_metrics` to find them and set
        DYNATRACE_ACE_METRIC_SELECTORS (or pass `metrics=[...]`).

        Args:
            nodes: One or more integration node names (matched against the ACE
                process-group entity names).
            metrics: Optional explicit metric selectors (overrides the configured
                ACE defaults).
            frm: Dynatrace timeframe start (default "now-24h").
            to: Dynatrace timeframe end (default "now").
        """
        names = _as_str_list(nodes)
        if not names:
            return _err('No node supplied. Pass nodes=["NODE1"].')
        selectors = _as_str_list(metrics) or DYNATRACE_ACE_METRIC_SELECTORS
        if not selectors:
            return _err(
                "No IBM ACE metric selectors configured. These are "
                "deployment-specific — run dynatrace_list_metrics (e.g. "
                'search_strings=["ace"]) to discover the keys, then set '
                "DYNATRACE_ACE_METRIC_SELECTORS or pass metrics=[...]."
            )
        return await _metrics_envelope(
            "nodes", names, "PROCESS_GROUP_INSTANCE", selectors, frm, to
        )

    @mcp.tool()
    @logged_tool
    async def dynatrace_problems(
        hostnames: list[str] | None = None,
        frm: str = "now-24h",
        status: str | None = None,
    ) -> str:
        """Dynatrace: Recent problems / anomalies / alerts over a time window.

        Lists Dynatrace problems (auto-detected anomalies and alerts) in the
        window, optionally scoped to one or more hosts, for incident correlation
        during triage. Answers "any problems on lodmq01 today", "what alerted in
        the last hour".

        Args:
            hostnames: Optional one or more host names to scope to (matched
                against HOST entity names). Omit for all problems in the window.
            frm: Dynatrace timeframe start (default "now-24h").
            status: Optional problem status filter — "OPEN" or "CLOSED".
        """
        hosts = _as_str_list(hostnames)
        entity_selector = None
        if hosts:
            quoted = ",".join(_dt_quote(h) for h in hosts)
            entity_selector = f'type("HOST"),entityName.in({quoted})'

        problems, error = await run_problems(
            entity_selector=entity_selector, frm=frm, status=status
        )
        if error is not None:
            return _err(error, hostnames=hosts or None)

        return json.dumps(
            {
                "status": "success",
                "hostnames": hosts or None,
                "from": frm,
                "status_filter": status,
                "count": len(problems),
                "message": (
                    f"{len(problems)} problem(s) found."
                    if problems
                    else "No problems in the time window."
                ),
                "problems": problems,
            },
            indent=2,
            default=str,
        )

    @mcp.tool()
    @logged_tool
    async def dynatrace_list_metrics(
        search_strings: list[str],
        limit: int = 50,
    ) -> str:
        """Dynatrace: Discover available metric keys (catalogue search).

        Searches the Dynatrace metric catalogue for keys whose name/description
        match the given term(s) and returns their `metricId`, display name, unit,
        and description. Use this to find the deployment-specific IBM MQ / ACE
        metric keys to pass to `dynatrace_mq_metrics` / `dynatrace_ace_metrics`
        (or to set DYNATRACE_MQ_METRIC_SELECTORS / DYNATRACE_ACE_METRIC_SELECTORS).

        Args:
            search_strings: One or more substrings to search the catalogue for
                (e.g. ["mq"], ["queue", "channel"], ["ace", "flow"]).
            limit: Max number of metric descriptors to return (default 50).
        """
        terms = _as_str_list(search_strings)
        if not terms:
            return _err('No search string supplied. Pass search_strings=["mq"].')

        # Dynatrace metric descriptor text search uses the `text()` transform.
        selector = " OR ".join(f'text("{t}")' for t in terms) if terms else None
        metrics, error = await list_metrics(text=selector, page_size=limit)
        if error is not None:
            return _err(error, search_strings=terms)

        return json.dumps(
            {
                "status": "success",
                "search_strings": terms,
                "count": len(metrics),
                "message": (
                    f"{len(metrics)} metric key(s) found."
                    if metrics
                    else "No matching metric keys in the catalogue."
                ),
                "metrics": metrics,
            },
            indent=2,
            default=str,
        )
