"""Offline coverage for the Dynatrace performance/problems tools and helpers.

No real HTTP. Exercises:
- Guard ordering in `run_metric_query`: unconfigured and disallowed-host calls
  are rejected BEFORE any network call, and upstream failures are sanitised
  (no raw body / token).
- `metric_stats` summary computation and `_dt_quote` selector-injection guard.
- Tool registration + `Dynatrace:` routing-prefix docstrings.
- Empty-arg and unconfigured-selector handling in the tool wrappers.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import mqacemcpserver as entry  # noqa: F401 — imports register the tools
from server import dynatrace_helpers, dynatrace_tools


def _tool(name: str):
    return entry.mcp._tool_manager._tools[name].fn


def _configure(monkeypatch, allow=("abc",)):
    """Make the helper appear configured + allow-listed for a fake host."""
    monkeypatch.setattr(dynatrace_helpers, "DYNATRACE_URL_BASE",
                        "https://abc12345.live.dynatrace.com")
    monkeypatch.setattr(dynatrace_helpers, "DYNATRACE_API_TOKEN", "dt-token")
    monkeypatch.setattr(dynatrace_helpers, "DYNATRACE_ALLOWED_HOSTNAME_PREFIXES",
                        list(allow))


# ---------------------------------------------------------------------------
# run_metric_query — guard ordering (no HTTP on rejection)
# ---------------------------------------------------------------------------
def test_metric_query_blocks_when_unconfigured(monkeypatch):
    def _boom():
        raise AssertionError("HTTP client must not be created when unconfigured")

    monkeypatch.setattr(dynatrace_helpers, "DYNATRACE_URL_BASE", "")
    monkeypatch.setattr(dynatrace_helpers, "DYNATRACE_API_TOKEN", "")
    monkeypatch.setattr(dynatrace_helpers, "get_http_client", _boom)
    series, err = asyncio.run(dynatrace_helpers.run_metric_query("builtin:host.cpu.usage"))
    assert series is None
    assert "not configured" in err


def test_metric_query_blocks_disallowed_host_before_http(monkeypatch):
    def _boom():
        raise AssertionError("HTTP client must not be created for blocked host")

    _configure(monkeypatch, allow=[])  # empty allow-list blocks the host
    monkeypatch.setattr(dynatrace_helpers, "get_http_client", _boom)
    series, err = asyncio.run(dynatrace_helpers.run_metric_query("builtin:host.cpu.usage"))
    assert series is None
    assert "not in the allowed list" in err


# ---------------------------------------------------------------------------
# run_metric_query — success + sanitised error paths (fake client)
# ---------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload: dict):
        self._payload = payload

    async def get(self, url, **kwargs):
        return _FakeResp(self._payload)


def test_metric_query_success_returns_results(monkeypatch):
    _configure(monkeypatch)
    payload = {"result": [{"metricId": "builtin:host.cpu.usage", "data": []}]}
    monkeypatch.setattr(dynatrace_helpers, "get_http_client", lambda: _FakeClient(payload))
    series, err = asyncio.run(dynatrace_helpers.run_metric_query("builtin:host.cpu.usage"))
    assert err is None
    assert series == [{"metricId": "builtin:host.cpu.usage", "data": []}]


def test_metric_query_error_is_sanitised(monkeypatch):
    _configure(monkeypatch)

    class _BadClient:
        async def get(self, url, **kwargs):
            raise RuntimeError("leak Api-Token=supersecret123")

    monkeypatch.setattr(dynatrace_helpers, "get_http_client", lambda: _BadClient())
    series, err = asyncio.run(dynatrace_helpers.run_metric_query("builtin:host.cpu.usage"))
    assert series is None
    assert "supersecret123" not in err  # raw exception/token never leaks
    assert "(ref " in err  # carries a correlation id


# ---------------------------------------------------------------------------
# metric_stats + _dt_quote
# ---------------------------------------------------------------------------
def test_metric_stats_computes_summary():
    result = {
        "metricId": "builtin:host.cpu.usage",
        "data": [
            {"dimensionMap": {"dt.entity.host": "HOST-1"},
             "values": [10.0, None, 20.0, 30.0]},
        ],
    }
    rows = dynatrace_helpers.metric_stats(result)
    assert len(rows) == 1
    row = rows[0]
    assert row["count"] == 3
    assert row["min"] == 10.0
    assert row["max"] == 30.0
    assert row["last"] == 30.0
    assert row["avg"] == 20.0


def test_dt_quote_escapes_selector_injection():
    assert dynatrace_helpers._dt_quote('lodmq01') == '"lodmq01"'
    # tilde operator char is stripped, then embedded quote is doubled
    assert dynatrace_helpers._dt_quote('a"b~c') == '"a""bc"'


# ---------------------------------------------------------------------------
# Tool registration + routing prefix
# ---------------------------------------------------------------------------
def test_dynatrace_tools_registered():
    names = set(entry.mcp._tool_manager._tools.keys())
    assert {
        "dynatrace_host_performance",
        "dynatrace_mq_metrics",
        "dynatrace_ace_metrics",
        "dynatrace_problems",
        "dynatrace_list_metrics",
    } <= names


def test_dynatrace_tool_docstrings_open_with_routing_prefix():
    for name in (
        "dynatrace_host_performance",
        "dynatrace_mq_metrics",
        "dynatrace_ace_metrics",
        "dynatrace_problems",
        "dynatrace_list_metrics",
    ):
        doc = _tool(name).__doc__ or ""
        assert doc.lstrip().startswith("Dynatrace:"), (
            f"{name} docstring must open with 'Dynatrace:' for LLM routing"
        )


# ---------------------------------------------------------------------------
# Tool wrappers — empty-arg / unconfigured-selector handling
# ---------------------------------------------------------------------------
def test_host_performance_empty_list_is_handled():
    out = json.loads(asyncio.run(_tool("dynatrace_host_performance")(hostnames=[])))
    assert out["status"] == "error"
    assert "No host supplied" in out["message"]


def test_mq_metrics_without_selectors_explains_discovery(monkeypatch):
    # MQ selectors default empty (deployment-specific) -> guidance, no HTTP.
    monkeypatch.setattr(dynatrace_tools, "DYNATRACE_MQ_METRIC_SELECTORS", [])
    out = json.loads(asyncio.run(_tool("dynatrace_mq_metrics")(qmgr_names=["QM1"])))
    assert out["status"] == "error"
    assert "dynatrace_list_metrics" in out["message"]


def test_host_performance_success_envelope(monkeypatch):
    async def _fake_query(metric_selector, entity_selector=None, frm="now-24h",
                          to="now", resolution=None):
        return [{
            "metricId": metric_selector,
            "data": [{"dimensionMap": {"dt.entity.host": "HOST-1"},
                      "values": [1.0, 2.0, 3.0]}],
        }], None

    monkeypatch.setattr(dynatrace_tools, "run_metric_query", _fake_query)
    monkeypatch.setattr(dynatrace_tools, "DYNATRACE_HOST_METRIC_SELECTORS",
                        ["builtin:host.cpu.usage"])
    out = json.loads(asyncio.run(
        _tool("dynatrace_host_performance")(hostnames=["lodmq01"])
    ))
    assert out["status"] == "success"
    assert out["count"] == 1
    assert out["series"][0]["avg"] == 2.0
