#!/usr/bin/env bash
# Jarvis MCP onboarding — one-command installer for Jupiter engineers.
#
# Usage (on your Mac, inside any directory):
#     bash <(curl -fsSL https://raw.githubusercontent.com/jupitermoney/jarvis/main/scripts/jarvis-onboard.sh)
#
# What it does:
#   1. Generates an SSH keypair for Jarvis MCP (or reuses an existing one)
#   2. Prints the DM you need to send to @Jarvis in Slack (with the right format)
#   3. After Rohit approves on the box, you paste your bearer (if HTTP) here
#   4. Prints ready-to-paste MCP config snippets for Claude Desktop / Cursor / Claude Code
#   5. Smoke-tests the connection
#
# Safe to re-run. No edits to your AI-tool config files — you copy/paste so you stay in control.
#
# Source: github.com/jupitermoney/jarvis · maintained by Rohit + Jarvis itself.

set -euo pipefail

JARVIS_BOX="ubuntu@3.6.202.121"
JARVIS_HTTP_LOCAL_PORT=8082
JARVIS_SLACK_UID="U0B37LWBP98"
SSH_KEY_DEFAULT="$HOME/.ssh/id_ed25519_jarvis"

# ─── pretty helpers ───────────────────────────────────────────────────────
bold()  { printf "\033[1m%s\033[0m\n" "$*"; }
green() { printf "\033[32m%s\033[0m\n" "$*"; }
yellow(){ printf "\033[33m%s\033[0m\n" "$*"; }
red()   { printf "\033[31m%s\033[0m\n" "$*"; }
hr()    { printf "─%.0s" $(seq 1 70); echo; }

require() {
    command -v "$1" >/dev/null 2>&1 || {
        red "ERROR: '$1' not found on \$PATH. Install it and re-run."
        exit 1
    }
}

trap 'red "Aborted."' INT

# ─── preflight ─────────────────────────────────────────────────────────────
echo
bold ":satellite_antenna:  Jarvis MCP onboarding"
hr
require ssh-keygen
require ssh
require curl
case "$(uname -s)" in
    Darwin)  PLATFORM=mac ;;
    Linux)   PLATFORM=linux ;;
    *)       red "Only macOS + Linux supported today."; exit 1 ;;
esac

# ─── 1. SSH key ────────────────────────────────────────────────────────────
bold "Step 1 of 5 — SSH key"
read -r -p "Path to your Jarvis SSH key (will create if missing) [$SSH_KEY_DEFAULT]: " SSH_KEY
SSH_KEY="${SSH_KEY:-$SSH_KEY_DEFAULT}"

if [[ ! -f "$SSH_KEY" ]]; then
    yellow "No key at $SSH_KEY — generating ed25519 keypair…"
    mkdir -p "$(dirname "$SSH_KEY")"
    ssh-keygen -t ed25519 -f "$SSH_KEY" -N "" -C "jarvis-mcp-$(whoami)-$(hostname -s)"
    green "  generated"
else
    green "  using existing $SSH_KEY"
fi
PUBKEY=$(cat "${SSH_KEY}.pub")
echo
echo "Your public key (this is what Jarvis needs):"
echo "  $PUBKEY"
echo

# ─── 2. Transport choice ───────────────────────────────────────────────────
bold "Step 2 of 5 — transport"
echo "  stdio: simplest, your local AI tool spawns the MCP server per session via SSH"
echo "  http:  long-running tunnel, bearer-authenticated; better for parallel sessions"
echo "  both:  set up both (recommended)"
read -r -p "Choice [stdio/http/both] (default: both): " TRANSPORT
TRANSPORT="${TRANSPORT:-both}"
case "$TRANSPORT" in stdio|http|both) ;; *) red "Bad choice."; exit 1 ;; esac

# ─── 3. The Slack DM ───────────────────────────────────────────────────────
bold "Step 3 of 5 — DM Jarvis"
hr
echo "Open Slack and DM the Jarvis bot. Copy this message verbatim:"
echo
yellow "──── COPY FROM HERE ────"
cat <<EOF
Hi, I'd like access to jarvis-mcp.

Transport: $(tr a-z A-Z <<<"$TRANSPORT")

Public key:
$PUBKEY

Please share the bearer token as well.
EOF
yellow "──── COPY TO HERE ────"
if [[ "$PLATFORM" = mac ]]; then
    {
        echo "Hi, I'd like access to jarvis-mcp."
        echo
        echo "Transport: $(tr a-z A-Z <<<"$TRANSPORT")"
        echo
        echo "Public key:"
        echo "$PUBKEY"
        echo
        echo "Please share the bearer token as well."
    } | pbcopy
    green "  copied to your clipboard via pbcopy."
fi
echo
echo "Send that DM to <@${JARVIS_SLACK_UID}> (Jarvis) in Slack."
echo "Rohit will approve your SSH key on the box. You'll get a DM back with confirmation"
echo "$( [[ "$TRANSPORT" != stdio ]] && echo "(and your bearer token if you asked for HTTP)" || echo "")."
echo
read -r -p "Press Enter once you've sent the DM and Rohit has confirmed approval (or Ctrl-C to bail): " _

# ─── 4. Bearer token (if HTTP) ─────────────────────────────────────────────
BEARER=""
if [[ "$TRANSPORT" != stdio ]]; then
    bold "Step 4 of 5 — paste your bearer"
    echo "Rohit's DM should have included a bearer token. Paste it here (input hidden):"
    read -r -s BEARER
    echo
    if [[ -z "$BEARER" ]]; then
        red "Empty bearer. Re-run when you have it."; exit 1
    fi
    green "  bearer received (${#BEARER} chars)"
fi

# ─── 5. Print MCP configs + smoke test ─────────────────────────────────────
bold "Step 5 of 5 — wire it into your AI tool"
hr
echo
bold "Claude Desktop  ($PLATFORM)"
case "$PLATFORM" in
    mac)   CONFIG_PATH="$HOME/Library/Application Support/Claude/claude_desktop_config.json" ;;
    linux) CONFIG_PATH="$HOME/.config/Claude/claude_desktop_config.json" ;;
esac
echo "  Edit: $CONFIG_PATH"
echo "  Add this under \"mcpServers\":"
echo
if [[ "$TRANSPORT" =~ ^(stdio|both)$ ]]; then
    cat <<EOF
    "jarvis-stdio": {
      "command": "ssh",
      "args": ["-i", "$SSH_KEY", "$JARVIS_BOX",
               "/home/ubuntu/jarvis/scripts/run_mcp_server.sh"]
    }
EOF
fi
if [[ "$TRANSPORT" =~ ^(http|both)$ ]]; then
    cat <<EOF
    "jarvis-http": {
      "url": "http://localhost:$JARVIS_HTTP_LOCAL_PORT/mcp/",
      "headers": { "Authorization": "Bearer $BEARER" }
    }
EOF
fi
echo
if [[ "$TRANSPORT" =~ ^(http|both)$ ]]; then
    echo "  HTTP requires an SSH tunnel running in a separate terminal:"
    echo "    ssh -i $SSH_KEY -L $JARVIS_HTTP_LOCAL_PORT:localhost:$JARVIS_HTTP_LOCAL_PORT $JARVIS_BOX"
    echo
fi

echo
bold "Cursor  ($PLATFORM)"
echo "  Edit: $HOME/.cursor/mcp.json  (create if missing — same shape as Claude Desktop)"

echo
bold "Claude Code CLI"
echo "  Run: claude mcp add jarvis-stdio ssh '$JARVIS_BOX' '/home/ubuntu/jarvis/scripts/run_mcp_server.sh' \\"
echo "        -- -i '$SSH_KEY'"

echo
hr
bold "Smoke test"
echo "Verifying your SSH key works against the box…"
if ssh -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=10 "$JARVIS_BOX" 'echo "ssh ok: $(date -u)"' 2>&1; then
    green "  ✓ SSH connection works"
else
    red "  ✗ SSH failed — most likely Rohit hasn't approved your key yet, or the bearer DM hasn't arrived."
    red "    Wait a few minutes, then re-run this script (it'll detect your existing key and continue)."
    exit 1
fi
if [[ "$TRANSPORT" =~ ^(http|both)$ ]]; then
    echo
    echo "Verifying HTTP MCP bearer (hits an auth-gated endpoint)…"
    # Use an actually-authenticated endpoint — /health is unauthed and would
    # pass with a wrong bearer. /api/v1/services/bullet-ms requires Bearer.
    HTTP_RC=$(ssh -i "$SSH_KEY" "$JARVIS_BOX" \
        "curl -sS -o /dev/null -w '%{http_code}' -H 'Authorization: Bearer $BEARER' \
            http://localhost:8081/api/v1/services/bullet-ms" 2>/dev/null || echo "??")
    case "$HTTP_RC" in
        200) green "  ✓ bearer works (lookup_service returned bullet-ms entry)" ;;
        401|403) red "  ✗ bearer was rejected (HTTP $HTTP_RC) — re-check the token Rohit DM'd you" ; exit 1 ;;
        *)   yellow "  ! got HTTP $HTTP_RC — server reachable but unexpected; ping Rohit if persistent" ;;
    esac
fi

echo
hr
green "All set. Three things to do now:"
echo
echo "  1. Paste the snippets above into your AI tool's MCP config (file paths shown per tool)"
echo "  2. *Fully quit and restart* the tool — Claude Desktop, Cursor, and Claude Code"
echo "     all load MCP config at startup. ⌘Q (Cmd+Q) is required, not just close-window."
if [[ "$TRANSPORT" =~ ^(http|both)$ ]]; then
echo "     If using HTTP transport: also open a terminal and run the SSH tunnel command"
echo "     shown above — it needs to stay running for the MCP session."
fi
echo "  3. Ask: \"List the indexed Jupiter repos.\" You should get ~240 back."
echo
echo "If anything looks off, DM <@${JARVIS_SLACK_UID}> with what you tried and what you got —"
echo "candid feedback >> politeness tax. To re-print just the config snippets later (e.g. you"
echo "lost them or restarted your tool), re-run this script — it detects your existing key and"
echo "skips the DM flow."
