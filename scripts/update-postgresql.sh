#!/usr/bin/env bash
# Updates an already initialized PostgreSQL deployment. Never upgrades the DB image.
set -Eeuo pipefail
umask 077
cd "$(dirname "${BASH_SOURCE[0]}")/.."
compose=(docker compose --profile scheduler --profile tools)
stage="validate configuration"
stopped=false
failed() {
  status=$?
  trap - ERR
  if [[ "$stopped" == true ]]; then
    "${compose[@]}" stop web scheduler >/dev/null 2>&1 || true
  fi
  echo "Update failed at: $stage (exit $status). Later stages were not run." >&2
  echo "No database volume was deleted or restored. Inspect backups/logs before restarting." >&2
  exit "$status"
}
trap failed ERR
"${compose[@]}" config --quiet
stage="pull application image"
"${compose[@]}" pull web scheduler migrate
stage="verify deployment pause is off"
"${compose[@]}" run --rm -T --no-deps migrate python -c 'import sys; from backend.utils.deployment_mode import maintenance_enabled; sys.exit(1 if maintenance_enabled() else 0)'
stage="stop web and scheduler"
stopped=true
"${compose[@]}" stop web scheduler
stage="backup"
export BACKUP_DIR=${BACKUP_DIR:-"$PWD/backups/$(date -u +%Y%m%dT%H%M%SZ)"}
bash scripts/backup-postgresql.sh
stage="schema migration"
"${compose[@]}" run --rm -T --no-deps migrate
stage="start web"
"${compose[@]}" up -d --no-deps web
stage="web health"
healthy=false
for ((attempt=0; attempt<30; attempt++)); do
  if "${compose[@]}" exec -T web python -c 'import json,urllib.request; r=json.load(urllib.request.urlopen("http://127.0.0.1:7860/health",timeout=3)); assert r["success"]' >/dev/null 2>&1; then
    healthy=true
    break
  fi
  sleep 2
done
[[ "$healthy" == true ]]
stage="start scheduler"
"${compose[@]}" up -d --no-deps scheduler
sleep 3
"${compose[@]}" ps --status running --services scheduler | grep -qx scheduler
stopped=false
echo "Update complete: Web healthy, scheduler running. Backup: $BACKUP_DIR"
echo "Verify public HTTPS/OAuth discovery and reconnect any first-migration OAuth clients."
