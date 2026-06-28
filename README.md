# MQ + ACE MCP platform

A mono-repo holding two independently deployable MCP server builds for **IBM MQ**
and **IBM App Connect Enterprise (ACE)** — with **Splunk log search** for
triage / root-cause — plus a self-contained chatbot stack that consumes them.
Each top-level folder is its own deliverable — own entry point, own
`requirements.txt`, deployable as a separate app/service.

## Components

| Folder | What it is | Detailed docs |
| --- | --- | --- |
| **[`mqacemcpserver/`](mqacemcpserver/README.md)** | The main unified MQ + ACE + Splunk MCP server (17 read-only diagnostic tools, one SSE endpoint). | [`mqacemcpserver/README.md`](mqacemcpserver/README.md) |
| **[`mqacemcpserver-single/`](mqacemcpserver-single/README.md)** | A second build exposing composite "single-call" tools (MQ/ACE/cert) plus the Splunk log-search tools. Runs side-by-side on its own port. | [`mqacemcpserver-single/README.md`](mqacemcpserver-single/README.md) |
| **[`backend/`](backend/README.md)** | Chatbot agent backend — FastAPI + LangGraph (OpenAI) on `:8002`, talks to an MCP server over SSE. | [`backend/README.md`](backend/README.md), [`backend/AGENTIC_AI.md`](backend/AGENTIC_AI.md) |
| **[`frontend/`](frontend/README.md)** | Chatbot UI — Streamlit (`:8003`), MCP-server-agnostic. | [`frontend/README.md`](frontend/README.md) |
| `scripts/` | PowerShell launchers (`start-all.ps1`, `start-streamlit.ps1`, `stop-all.ps1`) and ops tooling. Each `-Skip*` switch isolates a tier; no switches brings up the whole stack. | — |
| `resources/` | Shared CSV manifests (`qmgr_dump`, `node_config`, `node_dump`, `cert_dump`) consumed by **both** server builds. Replaced by a daily extract job. | — |
| `docs/` | Overview / supplementary docs: connecting clients, tool reference, narrative deck. | [`docs/README.md`](docs/README.md) |

## Shared vs. isolated

- **Shared at repo root:** `resources/` and the main build's dev `.venv`.
  Each server build auto-detects whether its **resource/log** defaults come from
  a standalone layout (its own `resources/` beside the code) or this mono-repo
  (the shared root `resources/`), so the same code deploys either way.
- **Isolated per component:** entry point, `requirements.txt`, and its **own
  `.env` / `.env.example` / `.env.example.linux`** and venv. Every component
  (both server builds included) reads only its own `<dir>/.env` — there is no
  repo-root `.env`. Deploy any one folder on its own by copying that folder's
  `.env.example` (or `.env.example.linux` on RHEL) to `.env`.

## Quick start (full local stack)

```powershell
# one-time: create the main build's venv at the repo root + install deps
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r mqacemcpserver\requirements.txt

# bring up MCP server (:8443) + chat backend (:8002) + Streamlit UI (:8003) + dashboard (:8004)
.\scripts\start-all.ps1
# stop everything
.\scripts\stop-all.ps1
```

Run just the MCP server:

```powershell
$env:MCP_TRANSPORT = "sse"
.venv\Scripts\python.exe mqacemcpserver\mqacemcpserver.py
```

See each component's README for setup, configuration, and deployment details.
Repo-specific guidance for Claude Code lives in [`CLAUDE.md`](CLAUDE.md).
