# AlphaSwarm — Deploy Runbook

From a blank Ubuntu VPS to the live site, step by step. This follows
`../DEPLOY_PLAN.md` (hosting choice, costs, and the payments truth:
Stripe is not available in Pakistan; real billing later via a
merchant of record such as Lemon Squeezy / Polar — until then the
platform runs in TEST-MODE billing and that does not block launch).

**Golden rule — admin first, public second.** The FIRST account to
sign up becomes the platform admin. The owner must create that
account *before* the site is reachable by the public (step 6 before
step 7). Do not swap the order.

Assumptions: Ubuntu 22.04/24.04 VPS (Hostinger KVM 1 or similar),
a `.com` domain whose DNS you control, root SSH access.

---

## 1. Server basics

```bash
apt update && apt upgrade -y
apt install -y python3 ufw unzip
# Python 3 standard library is all the app needs - no pip installs.
```

Create the unprivileged service user and the app directory:

```bash
useradd --system --create-home --shell /usr/sbin/nologin alphaswarm
mkdir -p /opt/alphaswarm
```

Firewall — only SSH and HTTP/HTTPS from the outside; the app port
(8000) stays closed to the world (Caddy talks to it on localhost):

```bash
ufw allow OpenSSH
ufw allow 80,443/tcp
ufw enable
```

## 2. Copy the code

From your machine, copy the platform folder (without the local data
dirs) to the server, e.g.:

```bash
rsync -av --exclude data --exclude data-demo --exclude __pycache__ \
  ~/workspace/alphaswarm-platform/ root@YOUR_VPS_IP:/opt/alphaswarm/
chown -R alphaswarm:alphaswarm /opt/alphaswarm
mkdir -p /opt/alphaswarm/data
chown alphaswarm:alphaswarm /opt/alphaswarm/data
```

## 3. Install the systemd service

```bash
cp /opt/alphaswarm/deploy/alphaswarm.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable alphaswarm
systemctl start alphaswarm
systemctl status alphaswarm        # expect: active (running)
curl -s http://127.0.0.1:8000/api/plans   # expect JSON with "test_mode": true
```

At startup the app also takes its first automatic backup into
`/opt/alphaswarm/data/backups/YYYY-MM-DD/` (daily after that, newest
7 days kept). HTTP access logs are capped by rotation in
`/opt/alphaswarm/data/logs/access.log` (1 MB × 3).

## 4. Point the domain at the server

At your registrar/DNS provider, create an **A record**:
`@` (or your subdomain) → the VPS IP. Wait until it resolves:

```bash
dig +short alphaswarm.example.com   # expect your VPS IP
```

(Use your real domain everywhere below — the files ship with the
placeholder `alphaswarm.example.com`.)

## 5. Install Caddy and enable HTTPS

```bash
apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  > /etc/apt/sources.list.d/caddy-stable.list
apt update && apt install -y caddy
```

Edit `/opt/alphaswarm/deploy/Caddyfile`: replace
`alphaswarm.example.com` with the real domain, then:

```bash
mkdir -p /var/log/caddy
cp /opt/alphaswarm/deploy/Caddyfile /etc/caddy/Caddyfile
systemctl reload caddy
```

**Do not announce the site yet** — nobody should reach signup before
step 6. (DNS propagation plus an unannounced domain keeps this safe
in practice; for extra care, do step 6 within the same session.)

## 6. Create the ADMIN account first (owner only)

Open `https://YOUR-DOMAIN/` and sign up with the owner's email.
**This first account automatically becomes the admin.** Confirm in
the Admin tab that the account shows role `admin`. Only after this:

## 7. Go public

- Check the demo flow once as a visitor would: sign up a second
  (test) account → it must be a normal user on the Free plan →
  subscribe through the TEST-MODE checkout → plan activates.
- Check Home / News / Billing render, and that the footer shows the
  Terms of Service and Privacy Policy links.
- Replace the placeholder contact address
  (`support@alphaswarm.example`) in the Terms/Privacy pages with the
  real support email (edit `static/index.html`, restart the service).
- Announce the site.

## 8. Day-to-day operations

- **Status / logs:** `systemctl status alphaswarm`,
  `journalctl -u alphaswarm -f`, app access log at
  `data/logs/access.log` (rotated, capped).
- **Backups:** automatic, daily, in `data/backups/YYYY-MM-DD/`
  (platform DB + every bot journal + news/alert logs, 7-day
  retention). Admin UI → Admin tab → *Create backup now* /
  *Download latest backup (.zip)*, or API:
  `POST /api/admin/backup`, `GET /api/admin/backup/download`
  (admin session required).
- **Restart after a code update:** copy the new code over
  `/opt/alphaswarm` (never overwrite `data/`), then
  `systemctl restart alphaswarm`. Data, users, bots and journals are
  all in `data/` and are preserved.
- **Kill switch:** Settings tab → global kill switch halts every bot
  and closes every open paper position immediately.

## 9. Restore from a backup

```bash
systemctl stop alphaswarm
cd /opt/alphaswarm/data
cp -a backups/YYYY-MM-DD/platform.db platform.db
# each bot journal:
cp -a backups/YYYY-MM-DD/bots/<bot_id>/journal.db bots/<bot_id>/journal.db
# optional: the shared news/alert logs
cp -a backups/YYYY-MM-DD/events.jsonl backups/YYYY-MM-DD/alerts.jsonl .
chown -R alphaswarm:alphaswarm /opt/alphaswarm/data
systemctl start alphaswarm
```

Verify: log in, check bots/users/trades match the backup date.

## 10. What is NOT in this launch (by design)

- **No live trading.** The platform is paper-trading only; there is
  no exchange-key collection and no real-order code path. That step
  needs a separate explicit decision plus a legal review first.
- **No real billing yet.** Checkout is TEST MODE. Real subscriptions
  go through a merchant of record that pays out to Pakistan (Lemon
  Squeezy or Polar — see `../DEPLOY_PLAN.md`); a US LLC + Stripe
  Atlas (~$500+) is deliberately deferred.
- Subscriptions should only be switched on for real after the paper
  record has been reviewed (the roadmap gate in the project docs).
