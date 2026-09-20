---
name: Jarvis resurrection runbook
description: How to fully rebuild Jarvis from zero if the box dies, the disk corrupts, or someone has to take this over. Comprehensive — includes deployed state, file inventory, dependencies, and recovery steps in dependency order.
type: project
originSessionId: b7218088-f9d8-46d9-9ad2-fe62fb99e7e9
---
This is the disaster-recovery + onboarding doc. If you're reading this and Jarvis is broken (or you're rebuilding from a wiped box), follow it top to bottom.

## What Jarvis IS today (as of 2026-05-12)

- An LLM-powered code assistant for Jupiter, indexed across **17 repos** in `github.com/jupitermoney`: `bff-core, platform, lms, gateway, jupiter, bullet, lending-orchestrator, cardboard, brahma, metal, mf-order-xpress, mf-explore-service, insurance-platform, bills, ppi-rail, ppi-pots, ppi-router`. Total ≈ 38,700 chunks in Qdrant.
- Accessible via `/jarvis <question>` slash command in Slack channel `C092S7Z5HB5` (allowlist-restricted, ephemeral-only). 6+ unique engineers using it daily, validated by Akhil/Armaan/Prasanna.
- 1 Slack bot process (systemd unit `jarvis-slack.service`) + 1 Qdrant docker container + 1 daily reindex timer (`jarvis-reindex.timer` at 22:00 UTC).

## Where everything lives

### Source of truth — local (Rohit's Mac)
`/Users/rohitpandey/Projects/jarvis/` — all Jarvis source code. Should be in git on GitHub.

```
scripts/
  indexer/                    # repo→chunks→Voyage→Qdrant pipeline
    config.py                 # all knobs: chunk size, batch limits, exclusions
    walker.py                 # file walker with skip rules
    chunker.py                # line-based + token-cap chunker
    embedder.py               # Voyage client w/ token-budget batching
    store.py                  # Qdrant collection + upsert
    main.py                   # entry: `python -m indexer.main <repo>` or `--all`
    query.py                  # ad-hoc retrieval test
  agent/                      # the agent loop + tools
    tools.py                  # search_code / read_file / list_repo_files
    agent.py                  # Sonnet 4.6 loop, system prompt, conversation history
    cli.py                    # `python -m agent.cli "question"` (terminal use)
    batch.py                  # batch runner for question files
    report.py                 # markdown→HTML report renderer
    usage_report.py           # parse slackbot.log → ranked usage report
  slackbot/                   # Slack Bolt app (Socket Mode)
    app.py                    # /jarvis handler, allowlist, sessions, threading
    format.py                 # markdown→Slack mrkdwn + Block Kit builders
  run_index.sh                # ad-hoc indexer launcher
  run_slackbot.sh             # bot launcher used by systemd
  reindex_all.sh              # daily reindex script (used by timer)
  jarvis-slack.service        # systemd unit for the bot
  jarvis-reindex.service      # systemd one-shot for reindex
  jarvis-reindex.timer        # daily 22:00 UTC schedule
docs/
  slack_announcement.md       # paste-ready announcement for engineering team
```

### Production — remote box
- **Host:** `ubuntu@3.6.202.121` · **SSH key:** `~/Downloads/data-science.pem` (on Rohit's Mac, chmod 600)
- **Working dir:** `~/jarvis/` on the box, with: `repos/` (cloned source), `index/qdrant_storage/` (vector data), `logs/`, `scripts/` (rsynced from local).
- **Env file:** `~/.config/jarvis/env` (chmod 600). Contains `VOYAGE_API_KEY`, `ANTHROPIC_API_KEY`, `SLACK_BOT_TOKEN` (`xoxb-…`), `SLACK_APP_TOKEN` (`xapp-…`), `JARVIS_ALLOWED_CHANNELS=C092S7Z5HB5`.

### External dependencies
- **Voyage AI** — embeddings (model `voyage-code-3`, dim 1024). Key in env. Acct: Rohit's. ~$0.18/1M tokens.
- **Anthropic** — LLM (model `claude-sonnet-4-6`). Key in env. Acct: Rohit's.
- **GitHub** — code source. `gh` is authed on the box as user `rkp2024` with scopes `gist, read:org, repo, workflow`. Token at `~/.config/gh/hosts.yml`.
- **Slack** — app called "Jarvis" in the Jupiter workspace `jupitermoney.enterprise.slack.com`. Socket Mode enabled. Bot scopes: `commands, chat:write, users:read`. App-level scope: `connections:write`. Slash command: `/jarvis`.

## Deployed state on the box

- **systemd:**
  - `jarvis-slack.service` (active, enabled) — bot, auto-restart on failure
  - `jarvis-reindex.timer` (active, enabled) — daily reindex schedule
  - `jarvis-reindex.service` (oneshot) — triggered by the timer
- **docker:**
  - `jarvis-qdrant` container, image `qdrant/qdrant:latest`, ports `127.0.0.1:6333` + `127.0.0.1:6334` (localhost-only, NOT exposed publicly), data volume at `~/jarvis/index/qdrant_storage`
- **Logs:**
  - `~/jarvis/logs/slackbot.log` — bot activity (every q/answer/reject)
  - `~/jarvis/logs/reindex.log` — daily reindex output
  - `~/jarvis/logs/index_*.log` — historical per-repo indexing logs

## Resurrection from zero (box died, fresh Ubuntu 24.04)

Assumes you have: SSH access to a new Ubuntu box, the local source dir, and the API keys (re-mint if lost).

```bash
# 0. From local Mac, SSH to new box
ssh -i ~/Downloads/data-science.pem ubuntu@<NEW_IP>

# 1. Install system tooling
sudo apt-get update
sudo apt-get install -y git jq python3 python3-venv python3-pip docker.io ripgrep fd-find
sudo usermod -aG docker ubuntu  # log out + in to take effect

# Install gh CLI
sudo mkdir -p -m 755 /etc/apt/keyrings
wget -nv -O- https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list
sudo apt-get update && sudo apt-get install -y gh

# 2. gh auth (interactive)
gh auth login   # GitHub.com → HTTPS → PAT with scopes: repo, read:org

# 3. Clone Jarvis source from GitHub (replace ORG/REPO with wherever it lives)
mkdir -p ~/jarvis
git clone https://github.com/jupitermoney/jarvis.git ~/jarvis-source
cp -r ~/jarvis-source/scripts ~/jarvis/scripts
cp -r ~/jarvis-source/docs ~/jarvis/docs
mkdir -p ~/jarvis/{repos,index/qdrant_storage,logs}

# 4. Python venv + deps
python3 -m venv ~/jarvis/scripts/indexer/.venv
~/jarvis/scripts/indexer/.venv/bin/pip install --upgrade pip
~/jarvis/scripts/indexer/.venv/bin/pip install -r ~/jarvis/scripts/indexer/requirements.txt slack-bolt

# 5. Restore env file (paste actual key values)
mkdir -p ~/.config/jarvis && chmod 700 ~/.config/jarvis
cat > ~/.config/jarvis/env <<'EOF'
export VOYAGE_API_KEY="..."
export ANTHROPIC_API_KEY="..."
export SLACK_BOT_TOKEN="xoxb-..."
export SLACK_APP_TOKEN="xapp-..."
export JARVIS_ALLOWED_CHANNELS="C092S7Z5HB5"
EOF
chmod 600 ~/.config/jarvis/env

# 6. Start Qdrant
docker run -d --name jarvis-qdrant --restart unless-stopped \
  -p 127.0.0.1:6333:6333 -p 127.0.0.1:6334:6334 \
  -v ~/jarvis/index/qdrant_storage:/qdrant/storage \
  qdrant/qdrant:latest
curl -fsS http://127.0.0.1:6333/healthz   # should print "healthz check passed"

# 7. Initial index — clones all 10 repos and embeds them. Takes ~30 min.
chmod +x ~/jarvis/scripts/*.sh
~/jarvis/scripts/reindex_all.sh

# 8. Install systemd units (bot + reindex timer)
chmod +x ~/jarvis/scripts/run_slackbot.sh
sudo install -m 644 ~/jarvis/scripts/jarvis-slack.service /etc/systemd/system/
sudo install -m 644 ~/jarvis/scripts/jarvis-reindex.service /etc/systemd/system/
sudo install -m 644 ~/jarvis/scripts/jarvis-reindex.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jarvis-slack.service jarvis-reindex.timer

# 9. Verify
systemctl status jarvis-slack jarvis-reindex.timer --no-pager
tail -10 ~/jarvis/logs/slackbot.log    # should show "⚡️ Bolt app is running!"

# 10. If new box has different IP, update Slack app config — Socket Mode means
#     no inbound URL is needed, so usually nothing to change Slack-side.
```

## Common operations

```bash
# Check bot health
systemctl status jarvis-slack
tail -f ~/jarvis/logs/slackbot.log

# Restart bot (e.g. after code change)
sudo systemctl restart jarvis-slack

# Trigger reindex on demand
sudo systemctl start jarvis-reindex.service
tail -f ~/jarvis/logs/reindex.log

# Usage report
source ~/.config/jarvis/env && cd ~/jarvis/scripts && \
  ./indexer/.venv/bin/python -m agent.usage_report

# Ad-hoc CLI query (no Slack)
source ~/.config/jarvis/env && cd ~/jarvis/scripts && \
  ./indexer/.venv/bin/python -m agent.cli --stats "your question here"

# Add a new repo to the index
# 1. Edit agent/tools.py INDEXED_REPOS list (add the repo name)
# 2. Edit agent/agent.py SYSTEM_PROMPT (add a one-liner description)
# 3. Edit slackbot/format.py help_blocks (add to the list)
# 4. Edit reindex_all.sh REPOS=(...) (add to the array)
# 5. Rsync, restart bot, run reindex
```

## Things that will bite you

1. **`pkill -f` matches your own SSH command line** if it contains the bot/script name. Always kill by exact PID, never with `-f` patterns containing `slackbot` or `indexer`.
2. **Voyage's tokenizer counts ~25-30% more than tiktoken** on some content. The `EMBED_BATCH_MAX_TOKENS` is set to 60,000 (well under Voyage's 120k limit) for that reason — don't raise it without testing.
3. **Ephemeral Slack messages can't be threaded.** Conversation continuity is implicit time-based (10-min idle window per user), not Slack-thread-based. Don't try to "fix" this with Slack threads — you'd lose ephemerality.
4. **Indexer kills mid-run leave partial chunks.** Re-running for the same repo wipes via `delete_repo()` first, so re-runs are safe.
5. **The reindex script does `rm -rf <repo>` then re-clones.** If a `read_file` tool call lands during the rm, it errors. Off-hours scheduling avoids this.

## How to take this over from Rohit

If Rohit isn't available and someone else needs to maintain Jarvis:
1. Get added to the Jupiter Slack workspace as an admin (or have Rohit transfer Slack app ownership).
2. Get SSH access to the box (ask infra for the pem or get added to authorized_keys).
3. Get added as a collaborator on the Jarvis GitHub repo (wherever it ends up).
4. Mint your own Voyage + Anthropic keys, swap into `~/.config/jarvis/env`, restart bot.
