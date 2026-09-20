#!/usr/bin/env bash
# Wrapper for jarvis-open-pr-digest.service. Sources env (follows
# feedback_systemd_env_pattern — never EnvironmentFile= on Python units).
set -eu
source /home/ubuntu/.config/jarvis/env
cd /home/ubuntu/jarvis/scripts
exec /home/ubuntu/jarvis/scripts/indexer/.venv/bin/python \
    /home/ubuntu/jarvis/scripts/open_pr_digest.py --post
