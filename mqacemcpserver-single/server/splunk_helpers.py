"""Splunk helpers: read-only SPL search client over the Splunk REST API.

All functions here are pure utilities — they do not register MCP tools.
The tool wrappers live in `server.splunk_tools`.

Safety/observability contracts honoured (same as the MQ and ACE halves):
- Every search passes through `is_unsafe_spl` (read-only SPL guard) BEFORE any call.
- The Splunk host is resolved and checked against `SPLUNK_ALLOWED_HOSTNAME_PREFIXES`
  BEFORE the HTTP request.
- The endpoint URL is recorded via `record_endpoint` for the per-call audit log.
- Every caught exception is routed through `safe_error_message` — the raw Splunk
  response body is never surfaced to the user.
"""
from __future__ import annotations

import json
from urllib.parse import urlparse

import httpx

from server.config import (
    SPLUNK_ALLOWED_HOSTNAME_PREFIXES,
    SPLUNK_PASSWORD,
    SPLUNK_URL_BASE,
    SPLUNK_USER,
)
from server.errors import safe_error_message
from server.logger import get_logger
from server.query_log import record_endpoint
from server.safety import SPL_BLOCKED_MSG, is_hostname_allowed, is_unsafe_spl

logger = get_logger("mqacemcpserver.splunk")

# ---------------------------------------------------------------------------
# Shared HTTP client
# ---------------------------------------------------------------------------
_HTTP_CLIENT: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    """Return a shared async HTTP client with Splunk basic auth."""
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None or _HTTP_CLIENT.is_closed:
        auth = httpx.BasicAuth(username=SPLUNK_USER, password=SPLUNK_PASSWORD)
        _HTTP_CLIENT = httpx.AsyncClient(verify=False, auth=auth, timeout=60.0)
    return _HTTP_CLIENT


async def aclose_http_client() -> None:
    global _HTTP_CLIENT
    if _HTTP_CLIENT is not None and not _HTTP_CLIENT.is_closed:
        await _HTTP_CLIENT.aclose()


def hostname_allowed(hostname: str) -> tuple[bool, str]:
    """Apply the Splunk-specific allow-list to a hostname."""
    return is_hostname_allowed(hostname, SPLUNK_ALLOWED_HOSTNAME_PREFIXES)


def _splunk_host() -> str:
    """Extract the hostname portion of SPLUNK_URL_BASE for the allow-list check."""
    return urlparse(SPLUNK_URL_BASE).hostname or ""


# ---------------------------------------------------------------------------
# SPL parsing / normalisation
# ---------------------------------------------------------------------------
def _normalise_spl(spl: str) -> str:
    """Ensure the SPL begins with an explicit `search` command.

    Splunk's REST search endpoint requires the leading `search` keyword (unlike
    the UI, which adds it implicitly). A query that already starts with a
    generating command (`| tstats`, `search ...`, `| ...`) is left untouched.
    """
    s = spl.strip()
    first = s.split(None, 1)[0].lower() if s else ""
    if s.startswith("|") or first in {"search", "tstats", "from", "makeresults"}:
        return s
    return f"search {s}"


# ---------------------------------------------------------------------------
# Read-only SPL search via the export endpoint
# ---------------------------------------------------------------------------
async def run_spl(
    spl: str,
    earliest: str = "-24h",
    latest: str = "now",
    max_count: int = 200,
) -> tuple[list[dict] | None, str | None]:
    """Run a read-only SPL search and return (results, error_message).

    On success: ``(list_of_result_dicts, None)``.
    On any guard rejection or failure: ``(None, user_safe_message)``.

    The raw exception/response body is never returned — failures go through
    ``safe_error_message`` (which logs the traceback with a request_id).
    """
    if is_unsafe_spl(spl):
        logger.warning("Blocked unsafe SPL: %s", spl)
        return None, SPL_BLOCKED_MSG

    host = _splunk_host()
    allowed, message = hostname_allowed(host)
    if not allowed:
        return None, message.strip()

    url = f"{SPLUNK_URL_BASE.rstrip('/')}/services/search/jobs/export"
    record_endpoint(url)

    data = {
        "search": _normalise_spl(spl),
        "output_mode": "json",
        "earliest_time": earliest,
        "latest_time": latest,
        "count": max_count,
    }

    try:
        response = await get_http_client().post(url, data=data, timeout=60.0)
        response.raise_for_status()
        return _parse_export_ndjson(response.text), None
    except Exception as err:
        msg = safe_error_message(
            err,
            hint="Splunk search failed",
            extra={"host": host, "earliest": earliest, "latest": latest},
        )
        return None, msg


def _parse_export_ndjson(text: str) -> list[dict]:
    """Parse the Splunk `export` ND-JSON stream into a list of result rows.

    Each non-empty line is a JSON object; data lines carry a ``result`` object
    (preview lines are skipped). Malformed lines are ignored defensively.
    """
    results: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and "result" in obj:
            if obj.get("preview") is True:
                continue
            results.append(obj["result"])
    return results


# ---------------------------------------------------------------------------
# Startup connectivity check
# ---------------------------------------------------------------------------
async def verify_connectivity() -> None:
    """Ping the Splunk REST API once at startup; log result. Never raises."""
    if not (SPLUNK_URL_BASE and SPLUNK_USER):
        return

    host = _splunk_host()
    allowed, _ = hostname_allowed(host)
    if not allowed:
        logger.info("Splunk host %s skipped (not in allow-list).", host)
        return

    url = f"{SPLUNK_URL_BASE.rstrip('/')}/services/server/info?output_mode=json"
    try:
        auth = httpx.BasicAuth(username=SPLUNK_USER, password=SPLUNK_PASSWORD)
        async with httpx.AsyncClient(verify=False, auth=auth) as client:
            resp = await client.get(url, timeout=5.0)
            if resp.status_code == 200:
                logger.info("Splunk REST API is responsive at %s.", SPLUNK_URL_BASE)
            elif resp.status_code == 401:
                logger.warning(
                    "Splunk REST API reachable but auth failed (HTTP 401) — "
                    "check SPLUNK_USER/SPLUNK_PASSWORD."
                )
            else:
                logger.warning(
                    "Splunk REST API returned HTTP %d at %s.",
                    resp.status_code, SPLUNK_URL_BASE,
                )
    except Exception as e:
        logger.warning("Cannot reach Splunk REST API at %s. Error: %s", url, e)
