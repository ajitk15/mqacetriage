"""Offline coverage for the Splunk log-search tools and helpers.

No real HTTP. Exercises:
- The read-only SPL guard (`is_unsafe_spl`) deny/allow lists.
- `run_spl` guard ordering: unsafe SPL and disallowed host are rejected BEFORE
  any network call, and upstream failures are sanitised (no raw body).
- ND-JSON parsing of the Splunk `export` stream.
- Tool registration + `Splunk:` routing-prefix docstrings.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import single_server as entry  # noqa: F401 — imports register the tools
from server import splunk_helpers
from server.safety import SPL_BLOCKED_MSG, is_unsafe_spl


def _tool(name: str):
    return entry.mcp._tool_manager._tools[name].fn


# ---------------------------------------------------------------------------
# is_unsafe_spl
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "spl",
    [
        "search index=ibm_mq error | delete",
        "index=x | outputlookup evil.csv",
        "index=x | collect index=summary",
        "index=x | sendemail to=a@b.com",
        "index=x | script python foo",
        "index=x | dump basefilename=out",
    ],
)
def test_is_unsafe_spl_blocks_mutating_commands(spl):
    assert is_unsafe_spl(spl) is True


@pytest.mark.parametrize(
    "spl",
    [
        "search index=ibm_mq AMQ9509",
        "index=ibm_ace BIP2230 | table _time host _raw",
        "index=x | stats count by host",
        "",
    ],
)
def test_is_unsafe_spl_allows_read_only(spl):
    assert is_unsafe_spl(spl) is False


# ---------------------------------------------------------------------------
# run_spl — guard ordering (no HTTP on rejection)
# ---------------------------------------------------------------------------
def test_run_spl_blocks_unsafe_before_http(monkeypatch):
    def _boom():
        raise AssertionError("HTTP client must not be created for unsafe SPL")

    monkeypatch.setattr(splunk_helpers, "get_http_client", _boom)
    events, err = asyncio.run(splunk_helpers.run_spl("index=x | delete"))
    assert events is None
    assert err == SPL_BLOCKED_MSG


def test_run_spl_blocks_disallowed_host_before_http(monkeypatch):
    def _boom():
        raise AssertionError("HTTP client must not be created for blocked host")

    monkeypatch.setattr(splunk_helpers, "get_http_client", _boom)
    monkeypatch.setattr(splunk_helpers, "SPLUNK_ALLOWED_HOSTNAME_PREFIXES", [])
    events, err = asyncio.run(splunk_helpers.run_spl("search index=x foo"))
    assert events is None
    assert "not in the allowed list" in err


# ---------------------------------------------------------------------------
# ND-JSON parsing
# ---------------------------------------------------------------------------
def test_parse_export_ndjson_collects_results_skips_preview():
    stream = "\n".join(
        [
            json.dumps({"preview": True, "result": {"_raw": "ignored preview"}}),
            json.dumps({"preview": False, "result": {"_raw": "AMQ9999E", "host": "loq1"}}),
            "not-json-garbage",
            json.dumps({"result": {"_raw": "AMQ9509E", "host": "loq2"}}),
            "",
        ]
    )
    rows = splunk_helpers._parse_export_ndjson(stream)
    assert [r["_raw"] for r in rows] == ["AMQ9999E", "AMQ9509E"]


# ---------------------------------------------------------------------------
# run_spl — success + sanitised error paths (fake client)
# ---------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self):
        return None


class _FakeClient:
    def __init__(self, text: str):
        self._text = text

    async def post(self, url, **kwargs):
        return _FakeResp(self._text)


def test_run_spl_success_parses_events(monkeypatch):
    text = json.dumps({"preview": False, "result": {"_raw": "AMQ9999E", "host": "loq1"}})
    monkeypatch.setattr(splunk_helpers, "get_http_client", lambda: _FakeClient(text))
    events, err = asyncio.run(splunk_helpers.run_spl("search index=ibm_mq AMQ9999"))
    assert err is None
    assert events == [{"_raw": "AMQ9999E", "host": "loq1"}]


def test_run_spl_error_is_sanitised(monkeypatch):
    class _BadClient:
        async def post(self, url, **kwargs):
            raise RuntimeError("super-secret splunk internal token=abc123")

    monkeypatch.setattr(splunk_helpers, "get_http_client", lambda: _BadClient())
    events, err = asyncio.run(splunk_helpers.run_spl("search index=ibm_mq AMQ9999"))
    assert events is None
    assert "abc123" not in err  # raw exception text never leaks
    assert "(ref " in err  # carries a correlation id


# ---------------------------------------------------------------------------
# Tool registration + routing prefix
# ---------------------------------------------------------------------------
def test_splunk_tools_registered():
    names = set(entry.mcp._tool_manager._tools.keys())
    assert {"splunk_search_logs", "splunk_mq_errors", "splunk_ace_errors"} <= names


def test_splunk_tool_docstrings_open_with_routing_prefix():
    for name in ("splunk_search_logs", "splunk_mq_errors", "splunk_ace_errors"):
        doc = _tool(name).__doc__ or ""
        assert doc.lstrip().startswith("Splunk:"), (
            f"{name} docstring must open with 'Splunk:' for LLM routing"
        )


def test_splunk_mq_errors_empty_list_is_handled():
    out = json.loads(asyncio.run(_tool("splunk_mq_errors")(qmgr_names=[])))
    assert out["status"] == "error"
    assert "No queue manager supplied" in out["message"]
