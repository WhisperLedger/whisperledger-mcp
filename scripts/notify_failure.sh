#!/usr/bin/env bash
# Slack alert sender, invoked by jarvis-failure-notify@.service.
# Argument $1 is the failed unit name (e.g. "jarvis-reindex.service").
#
# Reads SLACK_BOT_TOKEN + JARVIS_ALERTS_CHANNEL from /home/ubuntu/.config/jarvis/env.
# Falls back to the pilot channel (C092S7Z5HB5) if JARVIS_ALERTS_CHANNEL unset.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

if [ -f "$HOME/.config/astra/env" ]; then
    source "$HOME/.config/astra/env"
elif [ -f "$HOME/.config/jarvis/env" ]; then
    source "$HOME/.config/jarvis/env"
fi

eval $(python3 -c "
import sys
sys.path.append('$SCRIPT_DIR')
from agent.config import BOT_NAME, get_env
print(f'BOT_NAME=\"{BOT_NAME}\"')
print(f'ALERTS_CHANNEL=\"{get_env(\"ALERTS_CHANNEL\", \"C092S7Z5HB5\")}\"')
print(f'OWNER_UID=\"{get_env(\"OWNER_UID\", \"U0837N31T9C\")}\"')
print(f'SLACK_BOT_TOKEN=\"{get_env(\"SLACK_BOT_TOKEN\")}\"')
")

UNIT="${1:-unknown}"
CHANNEL="${ALERTS_CHANNEL}"
HOSTNAME=$(hostname)
NOW=$(date -u +"%Y-%m-%d %H:%M:%S UTC")

# Try to get the systemd-reported failure reason + journal tail.
SVC_STATUS=$(systemctl show "$UNIT" --property=ExecMainStatus,Result,ActiveEnterTimestamp 2>/dev/null \
    | tr '\n' ' ')
LOG_TAIL=$(sudo journalctl -u "$UNIT" --no-pager -n 12 --output=short 2>/dev/null | tail -12)
[[ -z "$LOG_TAIL" ]] && LOG_TAIL="(no journal entries available)"

# Truncate log tail if dangerously long for a Slack section block (3000 char hard cap).
if [[ ${#LOG_TAIL} -gt 2000 ]]; then
    LOG_TAIL="...(truncated)
${LOG_TAIL: -1800}"
fi

read -r -d '' BLOCK_TEXT <<EOF || true
:rotating_light: *${BOT_NAME} alert* — <@$OWNER_UID>

*Service:* \`$UNIT\`
*Host:* \`$HOSTNAME\`
*Time:* $NOW
*Status:* \`$SVC_STATUS\`

\`\`\`
$LOG_TAIL
\`\`\`

Run \`systemctl status $UNIT\` for full status, \`journalctl -u $UNIT\` for logs.
EOF

PAYLOAD=$(jq -nc \
    --arg channel "$CHANNEL" \
    --arg text ":rotating_light: ${BOT_NAME} service $UNIT failed on $HOSTNAME at $NOW" \
    --arg blocks_text "$BLOCK_TEXT" \
    '{
       channel: $channel,
       text: $text,
       blocks: [{ type: "section", text: { type: "mrkdwn", text: $blocks_text } }]
    }')

curl -fsS -X POST https://slack.com/api/chat.postMessage \
    -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
    -H "Content-Type: application/json; charset=utf-8" \
    -d "$PAYLOAD" >/tmp/astra_notify_response.json

# Surface API errors so they show up in the notify service's own log.
ok=$(jq -r '.ok' /tmp/astra_notify_response.json 2>/dev/null)
if [[ "$ok" != "true" ]]; then
    echo "Slack notify failed:" >&2
    cat /tmp/astra_notify_response.json >&2
    exit 1
fi
echo "alert sent for $UNIT to $CHANNEL"
