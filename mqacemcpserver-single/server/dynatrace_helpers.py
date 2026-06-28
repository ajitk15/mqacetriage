"""Dynatrace helpers: read-only client over the Dynatrace API v2.

All functions here are pure utilities — they do not register MCP tools.
The tool wrappers live in `server.dynatrace_tools`.

Safety/observability contracts honoured (same as the MQ, ACE and Splunk halves):
- The Dynatrace host is resolved and checked against
  `DYNATRACE_ALLOWED_HOSTNAME_PREFIXES` BEFORE any HTTP request.
- The endpoint URL is recorded via `record_endpoint` for the per-call audit log.
- Every caught exception is routed through `safe_error_message` — the raw
  Dynatrace response body (and the API token) is never surfaced to the user.

Read-only by construction: only Metrics / Entities / Problems API v2 **GET**
endpoints are called, so there is no write-command guard (the Splunk
`is_unsafe_spl` analogue) — we only sanitise user-supplied entity names that are
interpolated into an `entitySelector` (`_dt_quote`).
"""
from __future__ import annotations

from statistics import fmean
from urllib.parse import urlparse

import httpx

from server.config import (
    DYNATRACE_ALLOWED_HOSTNAME_PREFIXES,
    DYNATRACE_API_TOKEN,
    DYNATRACE_URL_BASE,
)
from server.errors import safe_error_message
from server.logger import get_logger
from server.query_log import record_endpoint
from server.safety import is_hostname_allowed

logger = get_logger("mqacemcpserver.dynatrace")

# ---------------------------------------------------------------------------
# Shared HTTP client
# ---------------------------------------------------------------------------
_HTTP_CLIENT: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    """Return a shared async HTTP client with the Dynatrace Api-Token header."""
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None or _HTTP_CLIENT.is_closed:
        headers = {"Authorization": f"Api-Token {DYNATRACE_API_TOKEN}"}
        _HTTP_CLIENT = httpx.AsyncClient(verify=False, headers=headers, timeout=60.0)
    return _HTTP_CLIENT


async def aclose_http_client() -> None:
    global _HTTP_CLIENT
    if _HTTP_CLIENT is not None and not _HTTP_CLIENT.is_closed:
        await _HTTP_CLIENT.aclose()


def hostname_allowed(hostname: str) -> tuple[bool, str]:
    """Apply the Dynatrace-specific allow-list to a hostname."""
    return is_hostname_allowed(hostname, DYNATRACE_ALLOWED_HOSTNAME_PREFIXES)


def _dt_host() -> str:
    """Extract the hostname portion of DYNATRACE_URL_BASE for the allow-list."""
    return urlparse(DYNATRACE_URL_BASE).hostname or ""


def _dt_quote(value: str) -> str:
    """Quote a user term for an entitySelector string literal.

    Dynatrace selector string literals are double-quoted; a literal `"` inside
    is escaped by doubling it (`""`). We also strip `~` which can act as a
    selector operator character. This blocks selector injection from a
    user-supplied host/QM/node name.
    """
    cleaned = str(value).replace("~", "").strip()
    return '"' + cleaned.replace('"', '""') + '"'


def _api_base() -> str:
    """Return the `/api/v2` base, tolerating a URL_BASE with or without it."""
    base = DYNATRACE_URL_BASE.rstrip("/")
    if base.endswith("/api/v2"):
        return base
    return f"{base}/api/v2"


# ---------------------------------------------------------------------------
# Pre-flight: allow-list gate shared by every public call
# ---------------------------------------------------------------------------
def _preflight() -> str | None:
    """Return a user-safe error message if the call must be blocked, else None."""
    if not (DYNATRACE_URL_BASE and DYNATRACE_API_TOKEN):
        return (
            "⚠️ Dynatrace is not configured. Set DYNATRACE_URL_BASE and "
            "DYNATRACE_API_TOKEN in this build's .env."
        )
    host = _dt_host()
    allowed, message = hostname_allowed(host)
    if not allowed:
        return message.strip()
    return None


# ---------------------------------------------------------------------------
# Metrics API v2 — timeseries query
# ---------------------------------------------------------------------------
async def run_metric_query(
    metric_selector: str,
    entity_selector: str | None = None,
    frm: str = "now-24h",
    to: str = "now",
    resolution: str | None = None,
) -> tuple[list[dict] | None, str | None]:
    """Run a read-only Metrics v2 query; return (series_list, error_message).

    On success: ``(list_of_result_objects, None)`` — each item is a Dynatrace
    metric ``result`` with ``metricId`` and ``data`` (dimensions / timestamps /
    values). On any guard rejection or failure: ``(None, user_safe_message)``.
    """
    blocked = _preflight()
    if blocked is not None:
        return None, blocked

    url = f"{_api_base()}/metrics/query"
    record_endpoint(url)

    params: dict[str, str] = {
        "metricSelector": metric_selector,
        "from": frm,
        "to": to,
    }
    if entity_selector:
        params["entitySelector"] = entity_selector
    if resolution:
        params["resolution"] = resolution

    try:
        response = await get_http_client().get(url, params=params, timeout=60.0)
        response.raise_for_status()
        payload = response.json()
        return payload.get("result", []), None
    except Exception as err:
        return None, safe_error_message(
            err,
            hint="Dynatrace metric query failed",
            extra={"host": _dt_host(), "metricSelector": metric_selector,
                   "from": frm, "to": to},
        )


# ---------------------------------------------------------------------------
# Entities API v2
# ---------------------------------------------------------------------------
async def resolve_entities(
    entity_selector: str,
    fields: str = "+properties.detectedName",
    page_size: int = 50,
) -> tuple[list[dict] | None, str | None]:
    """Resolve entities (HOST / PROCESS_GROUP_INSTANCE …) by selector."""
    blocked = _preflight()
    if blocked is not None:
        return None, blocked

    url = f"{_api_base()}/entities"
    record_endpoint(url)
    params = {"entitySelector": entity_selector, "fields": fields,
              "pageSize": str(page_size)}
    try:
        response = await get_http_client().get(url, params=params, timeout=30.0)
        response.raise_for_status()
        return response.json().get("entities", []), None
    except Exception as err:
        return None, safe_error_message(
            err, hint="Dynatrace entity lookup failed",
            extra={"host": _dt_host(), "entitySelector": entity_selector},
        )


# ---------------------------------------------------------------------------
# Problems API v2
# ---------------------------------------------------------------------------
async def run_problems(
    entity_selector: str | None = None,
    frm: str = "now-24h",
    to: str = "now",
    status: str | None = None,
    page_size: int = 50,
) -> tuple[list[dict] | None, str | None]:
    """Fetch problems (anomalies / alerts) over a window for correlation."""
    blocked = _preflight()
    if blocked is not None:
        return None, blocked

    url = f"{_api_base()}/problems"
    record_endpoint(url)
    params: dict[str, str] = {"from": frm, "to": to, "pageSize": str(page_size)}
    if entity_selector:
        params["entitySelector"] = entity_selector
    if status:
        params["problemSelector"] = f'status("{status}")'
    try:
        response = await get_http_client().get(url, params=params, timeout=30.0)
        response.raise_for_status()
        return response.json().get("problems", []), None
    except Exception as err:
        return None, safe_error_message(
            err, hint="Dynatrace problems query failed",
            extra={"host": _dt_host(), "from": frm, "to": to},
        )


# ---------------------------------------------------------------------------
# Metrics descriptor API v2 — discovery
# ---------------------------------------------------------------------------
async def list_metrics(
    text: str | None = None,
    page_size: int = 50,
) -> tuple[list[dict] | None, str | None]:
    """Search the metric catalogue for available metric keys + descriptions."""
    blocked = _preflight()
    if blocked is not None:
        return None, blocked

    url = f"{_api_base()}/metrics"
    record_endpoint(url)
    params: dict[str, str] = {
        "pageSize": str(page_size),
        "fields": "displayName,unit,description",
    }
    if text:
        params["metricSelector"] = text
    try:
        response = await get_http_client().get(url, params=params, timeout=30.0)
        response.raise_for_status()
        return response.json().get("metrics", []), None
    except Exception as err:
        return None, safe_error_message(
            err, hint="Dynatrace metric discovery failed",
            extra={"host": _dt_host(), "text": text or ""},
        )


# ---------------------------------------------------------------------------
# Series → compact stats
# ---------------------------------------------------------------------------
def metric_stats(result: dict) -> list[dict]:
    """Reduce one Metrics v2 ``result`` object to per-series avg/min/max/last.

    Each returned row has the resolved dimension(s), the metricId, and the
    summary statistics computed over the non-null datapoints in the window.
    """
    rows: list[dict] = []
    metric_id = result.get("metricId", "")
    for series in result.get("data", []):
        values = [v for v in (series.get("values") or []) if v is not None]
        dims = series.get("dimensionMap") or {}
        if not dims and series.get("dimensions"):
            dims = {"dimension": ", ".join(map(str, series["dimensions"]))}
        rows.append(
            {
                "metricId": metric_id,
                "dimensions": dims,
                "count": len(values),
                "avg": round(fmean(values), 4) if values else None,
                "min": round(min(values), 4) if values else None,
                "max": round(max(values), 4) if values else None,
                "last": round(values[-1], 4) if values else None,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Startup connectivity check (parity with the other halves; currently unwired)
# ---------------------------------------------------------------------------
async def verify_connectivity() -> None:
    """Ping the Dynatrace API once; log the result. Never raises."""
    if not (DYNATRACE_URL_BASE and DYNATRACE_API_TOKEN):
        return
    host = _dt_host()
    allowed, _ = hostname_allowed(host)
    if not allowed:
        logger.info("Dynatrace host %s skipped (not in allow-list).", host)
        return
    url = f"{_api_base()}/metrics?pageSize=1"
    try:
        headers = {"Authorization": f"Api-Token {DYNATRACE_API_TOKEN}"}
        async with httpx.AsyncClient(verify=False, headers=headers) as client:
            resp = await client.get(url, timeout=5.0)
            if resp.status_code == 200:
                logger.info("Dynatrace API is responsive at %s.", DYNATRACE_URL_BASE)
            elif resp.status_code in (401, 403):
                logger.warning(
                    "Dynatrace API reachable but auth failed (HTTP %d) — check "
                    "DYNATRACE_API_TOKEN and its scopes.", resp.status_code,
                )
            else:
                logger.warning(
                    "Dynatrace API returned HTTP %d at %s.",
                    resp.status_code, DYNATRACE_URL_BASE,
                )
    except Exception as e:
        logger.warning("Cannot reach Dynatrace API at %s. Error: %s", url, e)
