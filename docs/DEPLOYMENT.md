# Deployment (Hetzner CX22 or any Ubuntu 24.04 VM with ≥ 2 GB RAM)

Commands marked **laptop** run on your machine; everything else runs on the server.

## 1. Create the server
1. Create a Hetzner Cloud project → **Add Server**: Ubuntu 24.04, type **CX22**, add your SSH key.
   (On DigitalOcean, Lightsail or Oracle, pick Ubuntu 24.04 with ≥ 2 GB RAM.)
2. **Firewall:** allow inbound **22** only. Add 80/443 only if you will use the HTTPS proxy (step 7).

## 2. Harden and install Docker
```bash
ssh root@SERVER_IP
adduser --disabled-password --gecos "" deploy && usermod -aG sudo deploy
rsync --archive --chown=deploy:deploy ~/.ssh /home/deploy
sed -i 's/^#\?PasswordAuthentication .*/PasswordAuthentication no/; s/^#\?PermitRootLogin .*/PermitRootLogin no/' /etc/ssh/sshd_config
systemctl restart ssh
apt-get update && apt-get -y upgrade && apt-get -y install unattended-upgrades fail2ban
curl -fsSL https://get.docker.com | sh
usermod -aG docker deploy
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile \
  && echo '/swapfile none swap sw 0 0' >> /etc/fstab
```
Docker is enabled at boot. Every service uses `restart: unless-stopped`, so the stack comes back
after a reboot.

## 3. Copy the project
**laptop** (or push to a private Git repo and `git clone` on the server):
```bash
rsync -av --exclude .venv --exclude .git --exclude .env ai-job-alert-agent/ deploy@SERVER_IP:~/ai-job-alert-agent/
```

## 4. Configure secrets
```bash
ssh deploy@SERVER_IP
cd ~/ai-job-alert-agent
cp .env.example .env && chmod 600 .env
python3 -c "import secrets; [print(n+'='+secrets.token_urlsafe(32)) for n in ('BACKEND_API_KEY','N8N_ENCRYPTION_KEY','POSTGRES_PASSWORD')]"
nano .env   # paste those three, then set DASHBOARD_PASSWORD, GEMINI_API_KEY, GMAIL_*, NOTIFY_EMAIL_TO
```
Get the Gmail values by running `scripts/gmail_auth.py` on your **laptop** (see README).
**Do not change `N8N_ENCRYPTION_KEY` after first start.**

## 5. Start
```bash
docker compose up -d --build
docker compose ps            # db, backend, dashboard, n8n should be running/healthy
docker compose logs -f backend
```

## 6. Set up n8n and import the workflow
1. **laptop:** open an SSH tunnel to the n8n editor and the dashboard:
   ```bash
   ssh -N -L 5678:127.0.0.1:5678 -L 8501:127.0.0.1:8501 deploy@SERVER_IP
   ```
2. Open http://localhost:5678 and create the n8n owner account.
3. Import the workflow on the server:
   ```bash
   docker compose exec n8n n8n import:workflow --input=/workflows/job_monitor.json
   ```
4. In the n8n UI, open **AI Job Monitor** and click **Execute workflow** once. "Run monitoring
   cycle" should return a JSON summary. Then switch the workflow to **Active** (in newer n8n
   versions: **Publish**). It now ticks every minute; each source runs on its own interval.

## 7. (Optional) HTTPS access without a tunnel
Point DNS A records `dashboard.example.com` and `n8n.example.com` at the server. Set `DOMAIN`,
`ACME_EMAIL` and the `N8N_*` HTTPS variables in `.env`, open ports 80/443, then:
```bash
docker compose --profile proxy up -d
```
Caddy obtains TLS certificates automatically. The backend API is never exposed publicly.

## 8. Upload your resume and test email
In the dashboard (http://localhost:8501 through the tunnel):
1. **Resume** tab → upload your PDF → check the extracted skills.
2. **Source health** tab → **Send test email**. It should arrive in a few seconds, with the subject
   `NEW AI/ML JOB MATCH FOUND - Junior Machine Learning Engineer (TEST EMAIL)`.
3. Click **Run a monitoring cycle now**.

## 9. Verify the deployment
```bash
./scripts/verify_deployment.sh
```
It checks the containers, health endpoint, API-key enforcement, a forced live cycle, per-source
health, data persistence across a backend restart, and the workflow import.
Then confirm the schedule: after a few minutes, n8n → **Executions** should list one successful
run per minute, and the dashboard's **Source health** tab should show Greenhouse/Lever/Ashby
sources as *available* with a last successful check under 3 minutes ago.

## 10. Monitoring and maintenance
- **Healthchecks.io (recommended, free):** create a check with a 1-minute period and 10-minute
  grace, and put its ping URL in `HEALTHCHECKS_PING_URL`. If the VM, Docker, n8n or the backend
  dies, healthchecks.io emails you. This is the only alert that still works when the server
  itself is down.
- **Backups:** `crontab -e` → `0 3 * * * cd ~/ai-job-alert-agent && ./scripts/backup_db.sh`.
  Copy `backups/` off the server occasionally.
- **Logs:** `docker compose logs --since 1h backend`. Logs are rotated (5 × 10 MB per service).
- **Update:** `rsync` the new code, then run `docker compose up -d --build`.
- **Add companies:** edit `config/sources.yaml`, test it with
  `docker compose exec backend python scripts/check_sources.py lever`, then run
  `docker compose restart backend`.

## Troubleshooting
| Symptom | Fix |
|---|---|
| backend restarting, "BACKEND_API_KEY must be set" | Set a ≥ 24-char key in `.env`. |
| Email sources fail with `invalid_grant` | Refresh token expired or revoked. Publish the OAuth app to production (README step 6) and rerun `gmail_auth.py`. |
| `email:linkedin` healthy but finds 0 jobs | Make sure LinkedIn alerts reach this Gmail; check the sender address against `config/sources.yaml`. |
| LLM calls fail with 429 | The free-tier rate limit was hit. The job is still scored with keywords and nothing is lost; lower `MAX_LLM_CALLS_PER_DAY` or enable billing. |
| n8n "access to env vars denied" | `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` must be set (it is, in docker-compose.yml). |

## Upgrading an existing deployment (1–2 minute monitoring, Workday, more portals)

1. `git pull && docker compose up -d --build backend dashboard`. On startup the backend applies
   the new database migrations itself (`backend/migrations.py`: new columns, the cross-portal
   dedupe backfill, and a unique index on notifications). They are idempotent and keep all
   existing data. Jobs that were already emailed are protected from being emailed again.
   Take a backup first: `scripts/backup_db.sh`.
2. **Re-import the n8n workflow.** The old one still calls the synchronous cycle every 5 minutes:
   ```bash
   docker compose exec n8n n8n import:workflow --input=/workflows/job_monitor.json
   ```
   Then activate it again in the n8n UI and deactivate any older copy.
3. In `.env`, remove `CYCLE_INTERVAL_SECONDS=300` if you copied it from the old template (it
   only matters with the internal scheduler).
4. Set up the portal alert emails: [EMAIL_ALERTS.md](EMAIL_ALERTS.md). The new portals stay
   disabled until a real sample email has been verified.
5. Workday is on by default (`workday.enabled: true` in `config/sources.yaml`). Set it to
   `false` if you prefer not to use the career sites' no-login JSON endpoint.
6. After a few minutes, open dashboard → **Source health**. ATS sources should be *available*.
   Email sources show their delay only after the first alert email arrives.
