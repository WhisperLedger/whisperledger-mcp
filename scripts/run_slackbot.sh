#!/usr/bin/env bash
# Jarvis Slack bot launcher (used by systemd unit `jarvis-slack.service`).
# Sources env file (kept separate so manual CLI use also works) and execs the bot.
set -eu
source /home/ubuntu/.config/jarvis/env
exec /home/ubuntu/jarvis/scripts/indexer/.venv/bin/python -m slackbot.app
