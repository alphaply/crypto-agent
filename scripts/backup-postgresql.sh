#!/usr/bin/env bash
# Run from the deployment directory, after stopping web and scheduler.
set -Eeuo pipefail
umask 077
cd "$(dirname "${BASH_SOURCE[0]}")/.."
compose=(docker compose --profile scheduler --profile tools)
stage="verify stopped applications"
trap 'echo "Backup failed at: $stage. Applications were not started; keep partial files for diagnosis." >&2' ERR
running=$("${compose[@]}" ps --status running --services web scheduler)
if [[ -n "$running" ]]; then
  echo "Stop web and scheduler before backing up PostgreSQL and application files." >&2
  exit 1
fi
destination=${BACKUP_DIR:-"$PWD/backups/$(date -u +%Y%m%dT%H%M%SZ)"}
mkdir -p "$destination"
destination=$(cd "$destination" && pwd)
if [[ -e "$destination/COMPLETE" || -e "$destination/database.dump" ]]; then
  echo "Backup destination already used; select a new BACKUP_DIR." >&2
  exit 1
fi
stage="save deployment metadata"
cp .env "$destination/environment.env"
cp compose.yaml "$destination/compose.yaml"
container_ids=$("${compose[@]}" ps --all --quiet)
printf '[\n' > "$destination/images.json"
separator=""
while IFS= read -r container_id; do
  [[ -n "$container_id" ]] || continue
  printf '%s' "$separator" >> "$destination/images.json"
  # Container metadata remains available even if the previous image tag was
  # replaced or its image manifest was removed after pulling the new image.
  docker inspect --format '{"container":{{json .Name}},"image_id":{{json .Image}},"reference":{{json .Config.Image}}}' \
    "$container_id" >> "$destination/images.json"
  separator=","
done <<< "$container_ids"
printf ']\n' >> "$destination/images.json"
stage="PostgreSQL dump"
dump_name="server-$(date -u +%Y%m%dT%H%M%SZ)-$$.dump"
"${compose[@]}" run --rm -T --no-deps migrate uv run --no-sync python -m backend.storage.backup "/app/data/backups/$dump_name"
"${compose[@]}" run --rm -T --no-deps migrate cat "/app/data/backups/$dump_name" > "$destination/database.dump"
test -s "$destination/database.dump"
stage="application files"
"${compose[@]}" run --rm -T --no-deps migrate tar -C /app/data --exclude=./backups -czf - . > "$destination/application-data.tar.gz"
test -s "$destination/application-data.tar.gz"
printf '%s\n' "PostgreSQL dump + application data + environment + image metadata" > "$destination/COMPLETE"
echo "Backup complete: $destination (contains secrets; store privately)"
