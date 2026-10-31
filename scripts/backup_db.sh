#!/usr/bin/env bash
# Daily Postgres backup (bundled db only). Cron: 0 3 * * * cd ~/ai-job-alert-agent && ./scripts/backup_db.sh
set -euo pipefail
mkdir -p backups
docker compose exec -T db pg_dump -U "${POSTGRES_USER:-jobagent}" "${POSTGRES_DB:-jobagent}" | gzip > "backups/jobagent-$(date +%F).sql.gz"
find backups -name '*.sql.gz' -mtime +14 -delete
