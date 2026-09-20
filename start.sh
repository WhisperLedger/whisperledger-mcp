#!/usr/bin/env bash
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

echo "===================================================="
echo "⚡ Starting WhisperLedger MCP Server & REST Gateway"
echo "===================================================="
echo "Port: 5005"
echo "Mode: Stdio JSON-RPC (mcp_server.py) + REST API (api_server.py)"

python3 api_server.py
