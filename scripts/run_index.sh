#!/usr/bin/env bash
# Re-runnable indexer launcher. Run detached so it survives SSH disconnects.
# Usage:  ./run_index.sh repo1 repo2 ...
# Logs to ~/jarvis/logs/index_run.log
set -u
source ~/.config/jarvis/env
cd ~/jarvis/scripts

ts() { date +%H:%M:%S; }

echo "########## RUN START $(ts) pid=$$ args=$* ##########"
for r in "$@"; do
  echo ""
  echo "########## $(ts) $r ##########"
  ./indexer/.venv/bin/python -m indexer.main "$r"
done
echo ""
echo "########## ALL DONE $(ts) ##########"
curl -fsS http://127.0.0.1:6333/collections/jarvis_code | jq '{points: .result.points_count, status: .result.status}'
du -sh ~/jarvis/index/qdrant_storage
