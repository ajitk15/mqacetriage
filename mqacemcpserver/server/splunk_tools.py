"""Splunk (read-only log search) MCP tool registrations.

Each tool's docstring starts with "Splunk:" so the orchestrator's LLM can
unambiguously route log-search intents away from MQ ("IBM MQ:"), ACE
("IBM ACE:"), and certificate ("Certificate:") tools. Tool names are prefixed
with `splunk_`.

All tools are read-only: every SPL string is screened by `is_unsafe_spl`
(in `server.splunk_helpers.run_spl`) before any call, and the Splunk host is
checked against the allow-list. These tools answer the historical "what went
wrong / why did it fail" questions that the live MQ/ACE tools cannot — the
orchestrator can pair a Splunk error search with a live `mq_*`/`ace_*`
inspection to produce a root-cause narrative.
"""
from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from server.config import SPLUNK_ACE_INDEX, SPLUNK_MQ_INDEX
from server.logger import get_logger
from server.query_log import logged_tool
from server.splunk_helpers import run_spl

logger = get_logger("mqacemcpserver.splunk.tools")

# Fields surfaced per event — enough to triage without dumping the full record.
_EVENT_FIELDS = "_time, host, source, sourcetype, _raw"


def _as_str_list(value) -> list[str]:
    """Normalise a list/str argument to a clean, de-duplicated list of strings."""
    if value is None:
        return []
    items = [value] if isinstance(value, str) else list(value)
    cleaned = [str(v).strip() for v in items if v is not None and str(v).strip()]
    return list(dict.fromkeys(cleaned))


def _spl_quote(value: str) -> str:
    """Quote a user-supplied term as an SPL string literal (escapes quotes)."""
    return '"' + value.replace('"', '\\"') + '"'


def _or_terms(terms: list[str]) -> str:
    """Build an `("a" OR "b")` SPL fragment from quoted terms."""
    return "(" + " OR ".join(_spl_quote(t) for t in terms) + ")"


async def _search_envelope(
    spl: str, earliest: str, latest: str, max_count: int = 200
) -> str:
    """Run an SPL search and return a JSON envelope string (success or error)."""
    events, error = await run_spl(spl, earliest, latest, max_count)
    if error is not None:
        return json.dumps({"status": "error", "message": error, "spl": spl}, indent=2)

    return json.dumps(
        {
            "status": "success",
            "spl": spl,
            "earliest": earliest,
            "latest": latest,
            "count": len(events),
            "message": (
                f"{len(events)} event(s) found."
                if events
                else "No matching events in the time window."
            ),
            "events": events,
        },
        indent=2,
        default=str,
    )


def register(mcp: FastMCP) -> None:
    """Attach every Splunk tool to the given FastMCP instance."""

    @mcp.tool()
    @logged_tool
    async def splunk_search_logs(
        search_strings: list[str],
        source_type: str | None = None,
        earliest: str = "-24h",
        latest: str = "now",
    ) -> str:
        """Splunk: Search the centralised MQ + ACE logs for one or more terms.

        Runs a read-only SPL search across the configured MQ and ACE indexes and
        returns the matching events (`_time`, `host`, `source`, `sourcetype`,
        `_raw`). Use this for historical questions the live tools cannot answer —
        e.g. "any errors mentioning QL.ORDERS yesterday", "show AMQ9999 events",
        "what did NODE1 log around 14:00".

        Pass MULTIPLE search strings to match ANY of them in one call — e.g.
        `search_strings=["QL.ORDERS", "AMQ9509"]`.

        Args:
            search_strings: One or more substrings/terms to match (OR-combined).
            source_type: Optional Splunk sourcetype to scope to. OMIT this
                unless the user EXPLICITLY names a sourcetype — the index scope
                already narrows to the MQ/ACE data, and sourcetype names are
                deployment-specific, so guessing one almost always returns zero
                hits. Only pass a value the user actually gave you.
            earliest: Splunk time modifier for the start of the window
                (default "-24h"). Examples: "-1h", "-7d", "@d".
            latest: Splunk time modifier for the end of the window
                (default "now").
        """
        terms = _as_str_list(search_strings)
        if not terms:
            return json.dumps(
                {
                    "status": "error",
                    "message": "No search string supplied. Pass search_strings=[\"...\"].",
                },
                indent=2,
            )

        index_clause = f"(index={SPLUNK_MQ_INDEX} OR index={SPLUNK_ACE_INDEX})"
        st_clause = f" sourcetype={_spl_quote(source_type)}" if source_type else ""
        spl = (
            f"{index_clause}{st_clause} {_or_terms(terms)} "
            f"| table {_EVENT_FIELDS}"
        )
        return await _search_envelope(spl, earliest, latest)

    @mcp.tool()
    @logged_tool
    async def splunk_mq_errors(
        qmgr_names: list[str],
        earliest: str = "-24h",
    ) -> str:
        """Splunk: Recent IBM MQ error-log events (AMQ codes) for one or more queue managers.

        Canned search over the MQ error-log index for AMQ* diagnostic messages
        scoped to the given queue manager(s). Answers "why is QM1 failing",
        "any MQ errors on QM1 in the last hour", "what AMQ codes fired on QM1".

        Pass MULTIPLE queue managers to cover them all in one call — e.g.
        `qmgr_names=["MQQMGR1", "MQQMGR2"]`.

        Args:
            qmgr_names: One or more queue manager names (matched as terms in the
                MQ error-log events).
            earliest: Splunk time modifier for the start of the window
                (default "-24h"). Examples: "-1h", "-7d".
        """
        qms = _as_str_list(qmgr_names)
        if not qms:
            return json.dumps(
                {
                    "status": "error",
                    "message": "No queue manager supplied. Pass qmgr_names=[\"QM1\"].",
                },
                indent=2,
            )

        spl = (
            f"index={SPLUNK_MQ_INDEX} {_or_terms(qms)} "
            f"(\"AMQ\" OR error OR ERROR) "
            f"| table {_EVENT_FIELDS}"
        )
        return await _search_envelope(spl, earliest, latest="now")

    @mcp.tool()
    @logged_tool
    async def splunk_ace_errors(
        nodes: list[str],
        earliest: str = "-24h",
    ) -> str:
        """Splunk: Recent IBM ACE error events (BIP codes / syslog) for one or more integration nodes.

        Canned search over the ACE log index for BIP* diagnostic messages and
        error-level syslog scoped to the given integration node(s). Answers
        "why did NODE1 stop", "any BIP errors on NODE1 today", "what failed on
        the integration server".

        Pass MULTIPLE nodes to cover them all in one call — e.g.
        `nodes=["NODE1", "NODE2"]`.

        Args:
            nodes: One or more integration node names (matched as terms in the
                ACE log events).
            earliest: Splunk time modifier for the start of the window
                (default "-24h"). Examples: "-1h", "-7d".
        """
        names = _as_str_list(nodes)
        if not names:
            return json.dumps(
                {
                    "status": "error",
                    "message": "No node supplied. Pass nodes=[\"NODE1\"].",
                },
                indent=2,
            )

        spl = (
            f"index={SPLUNK_ACE_INDEX} {_or_terms(names)} "
            f"(\"BIP\" OR error OR ERROR) "
            f"| table {_EVENT_FIELDS}"
        )
        return await _search_envelope(spl, earliest, latest="now")
