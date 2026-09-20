# WhisperLedger MCP (Model Context Protocol) Server & Autonomous Operator

> Central AI Copilot, MCP Server, and Infrastructure Orchestrator for the **WhisperLedger** organization under **Pitcher**.

---

## ⚡ Overview

`whisperledger-mcp` is an autonomous engineering operator and standardized Model Context Protocol (MCP 2.0) server. It connects Claude Desktop, Cursor, Antigravity IDE, and the **Pitcher Console** (`pitcher-console`) to the complete WhisperLedger ecosystem.

### What It Does
1. **Deep Codebase Inspection**: Contextual architecture awareness across `whisperledger-backend` (Go Hexagonal, 3-way outflow, minimum cash flow debt graph solver), `whisperledger-frontend` (Expo 51, Kotlin SMS bridge, Biometric security gate), `whisperledger-web` (React 18 portal), and `pitcher-console` (Fleet command center).
2. **Infrastructure Telemetry**: Instant live telemetry and probe verification across Render, Neon Serverless PostgreSQL, Cloudflare Global CDN, and Expo EAS.
3. **Pull Request Automation**: Autonomous branch creation and Pull Request generation respecting GitHub branch protection rules.
4. **Zero-Downtime Deployment & Rollback**: Safe release pipelines with canary checks.
5. **Security & Governance Audits**: Real-time validation of branch protection (`main`), secret posture, and strict CORS configuration.
6. **Unified Grep Code Search**: Fast multi-repo symbol and keyword discovery.

---

## 🛠️ MCP Tools Reference

| Tool | Parameters | Description |
|---|---|---|
| `whisperledger_query_codebase` | `repo` (str), `topic` (str) | Deep technical architecture lookup (3-way outflow, debt graph, SMS bridge, pgx pool) |
| `whisperledger_get_infra_status` | `environment` ("staging" \| "production") | Query service health, latency, database pool status, and ₹0.00 cost monitoring |
| `whisperledger_create_pull_request` | `repo` (str), `branch_name` (str), `title` (str), `body` (str) | Generate feature branch and submit PR via GitHub CLI |
| `whisperledger_trigger_deployment` | `service` (str), `action` ("deploy" \| "rollback"), `version` (str) | Trigger blue/green or canary deployment verification |
| `whisperledger_audit_security` | *None* | Verifies branch protection rules, secrets integrity, and CORS policies |
| `whisperledger_search_code` | `query` (str), `repo` (optional str) | Cross-repository regex and symbol search |

---

## 🚀 Getting Started

### Prerequisites
- Python 3.10+
- GitHub CLI (`gh`) logged in to WhisperLedger organization

### 1. Run MCP Server in Stdio Mode (Claude Desktop / Cursor / Antigravity)
Add this to your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "whisperledger": {
      "command": "python3",
      "args": [
        "/Users/tanmayagarwal/TanmayProjects/whisperledger-mcp/mcp_server.py"
      ]
    }
  }
}
```

### 2. Run MCP Server in HTTP / REST Mode (Pitcher Console & Webhook integration)
```bash
./start.sh
# Server starts at http://0.0.0.0:5005
```

### 3. Docker Deployment
```bash
docker compose up -d --build
```

---

## 📡 HTTP & JSON-RPC Endpoints

- `GET /health`: Healthcheck probe (`HTTP 200 OK`)
- `GET /api/tools`: Catalog of available MCP tools and schemas
- `GET /api/status`: Unified status of infrastructure and security policies
- `POST /api/chat`: Natural language copilot query processor used directly by Pitcher Console
- `POST /api/execute`: Direct tool execution payload `{"tool": "whisperledger_query_codebase", "args": {...}}`
- `POST /mcp`: Standard JSON-RPC 2.0 endpoint (`tools/list`, `tools/call`, `initialize`)

---

## 🔒 Security & Branch Protection

Direct push to `main` is **strictly prohibited** across all organization repositories:
- `WhisperLedger/whisperledger-backend`
- `WhisperLedger/whisperledger-frontend`
- `WhisperLedger/whisperledger-web`
- `WhisperLedger/pitcher-console`
- `WhisperLedger/whisperledger-mcp`

All changes must be submitted via feature branch and approved Pull Request.
