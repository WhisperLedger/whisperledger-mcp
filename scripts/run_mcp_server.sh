#!/usr/bin/env bash
# Jarvis MCP server launcher. Two transports:
#
#   ./run_mcp_server.sh                        → stdio  (Phase 1; spawned per-session)
#   ./run_mcp_server.sh --transport=http       → streamable-http on 127.0.0.1:8082 (Phase 2)
#
# Stdio usage (local MCP client like Claude Desktop / Cursor / Claude Code):
#   command: ssh
#   args:    ["-i", "<your-key.pem>", "ubuntu@3.6.202.121",
#             "/home/ubuntu/jarvis/scripts/run_mcp_server.sh"]
#
# HTTP usage (via jarvis-mcp.service systemd unit on the box):
#   Engineer tunnels:   ssh -L 8082:localhost:8082 ubuntu@3.6.202.121
#   Client config:      url: http://localhost:8082/mcp/   header: Authorization: Bearer $JARVIS_API_KEY
#
# Sources ~/.config/jarvis/env via the bash-source pattern (follows
# feedback_systemd_env_pattern — never use systemd EnvironmentFile= on Python units).
set -eu

if [ -f "$HOME/.config/astra/env" ]; then
    source "$HOME/.config/astra/env"
elif [ -f "$HOME/.config/jarvis/env" ]; then
    source "$HOME/.config/jarvis/env"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
exec "$SCRIPT_DIR/indexer/.venv/bin/python" -m astra_mcp.server "$@"
