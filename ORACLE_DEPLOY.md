# AlphaSwarm — Oracle Cloud Always Free Deploy Addendum

Prepared 2026-11-20. This is the **$0/month alternative** to the
Hostinger plan in `DEPLOY_PLAN.md`. It reuses the existing deploy
package (`deploy/alphaswarm.service`, `deploy/Caddyfile`, the app's
built-in daily backups) unchanged — only the server source, the DNS
name, and a few Oracle-specific rules differ. For everything not
mentioned here, follow `deploy/DEPLOY_RUNBOOK.md` as written.

Facts below were checked against Oracle's own pages on 2026-11-20.
Where a console label or flow is described from general knowledge
rather than verified, it is marked **[verify in console]** — check
the wording on screen before clicking through.

**Golden rule — unchanged: admin first, public second.** The FIRST
account to sign up becomes the platform admin. Create it before the
address is shared with anyone (runbook step 6 before step 7).

---

## 1. What Always Free actually gives (2026-11-20 figures)

- **ARM Ampere A1:** 2 OCPUs + 12 GB RAM (this was 4 OCPU / 24 GB
  until Oracle cut it in June 2026 — ignore older blog posts).
- **Storage:** 200 GB total block storage across all volumes.
- **Network:** 10 TB outbound per month.
- **Plus:** 2 small AMD micro VMs (1 GB RAM each) — not needed for
  this platform; leave them unused.
- **Permanent:** Always Free has no end date and no trial clock.
  The separate 30-day $300 trial credit exists alongside it, but
  the platform must be built **only** on Always Free shapes so
  nothing starts billing when the trial ends.

For AlphaSwarm (pure Python stdlib, SQLite, hourly bot cycles) this
is more than enough — it replaces the Hostinger KVM 1 in
`VISION.md` §6 Phase S1 at $0 instead of ~$6.49–11.99/mo.

## 2. Owner signup checklist (Sarosh — the only part only you can do)

Have ready before starting at oracle.com/cloud/free:

- [ ] An email address you control (account notices and the
      verification mail go here).
- [ ] A payment card in your name. Oracle places a **small
      temporary verification hold** (typically ~US$1, refunded
      automatically) to confirm identity. It is a hold, not a
      charge, and Always Free usage is never billed. Enter card
      details only on Oracle's own signup pages — never in chat.
- [ ] A phone number for account verification **[verify in
      console — Oracle may ask for SMS verification]**.

**Home region — choose carefully, it is PERMANENT.** The home
region picked at signup cannot be changed afterwards for Always
Free resources. Pick in this order:

1. **Mumbai** — closest to Pakistan, first choice.
2. **Hyderabad** — also India, nearly as close.
3. **Frankfurt** — fallback if the India regions show no free A1
   capacity at instance-creation time.

Capacity warning: free A1 instances are scarce in popular regions.
"Out of capacity" at creation time is common and is **not** a
rejection — the fix is to retry later or use the fallback region
above; it is not a reason to upgrade the account.

One account per person — do not create a second account to work
around capacity; Oracle treats duplicates as abuse.

## 3. Create the instance

In the OCI console → Compute → Instances → Create instance
**[verify in console — exact menu labels]**:

- **Name:** `alphaswarm`
- **Image:** Ubuntu (Canonical Ubuntu 24.04 or the newest LTS
  offered) — **ARM/aarch64 build** of the image.
- **Shape:** `VM.Standard.A1.Flex` (Ampere) — set **2 OCPUs,
  12 GB RAM**. This shape is the Always Free one; if the console
  shows a cost estimate other than $0 / "Always Free eligible",
  stop and re-check the shape before creating.
- **Boot volume:** 50 GB is plenty (app + data + backups are
  small) and leaves most of the 200 GB allowance free. Do not
  take the whole 200 GB.
- **Networking:** place it in the default VCN/subnet created by
  the wizard; assign a public IPv4 address.
- **SSH key:** choose "Generate a key pair for me" (or upload a
  public key) and **download the private key file immediately** —
  Oracle does not let you download it again. Keep the private key
  file somewhere safe and private. It is a credential: it reaches
  Muse only through a secure route, never pasted into chat, and
  the deploy proceeds only with what the owner provides that way.

## 4. Open the right ports (OCI firewall)

OCI filters traffic at the VCN level (Security Lists / Network
Security Groups) *in addition to* the Ubuntu firewall in the
runbook. Allow inbound:

| Port | Source | Purpose |
|------|--------|---------|
| 22   | Your home IP if it is stable, otherwise 0.0.0.0/0 | SSH |
| 80   | 0.0.0.0/0 | HTTP (Caddy → HTTPS redirect + cert issue) |
| 443  | 0.0.0.0/0 | HTTPS |

Nothing else. In particular **do not open 8000** — the app binds
127.0.0.1 and only Caddy talks to it, exactly as on Hostinger.
Add these as ingress rules on the subnet's security list or an NSG
attached to the instance **[verify in console — OCI offers both
mechanisms; either is fine]**.

Note: OCI blocks outbound port 25 by default. Irrelevant here —
the platform sends no email.

Then apply the runbook's `ufw` rules on the instance itself
(runbook §1) as the second layer.

## 5. Deploy — same package, two Oracle differences

Follow `deploy/DEPLOY_RUNBOOK.md` steps 1–3 and 5–7 as written,
with these substitutions:

- **Server address:** the OCI instance's public IP instead of the
  Hostinger VPS IP (runbook §2 `rsync` target, §4 DNS).
- **Domain:** the free subdomain from §6 below instead of a .com,
  until a domain is bought.
- **Architecture:** the instance is ARM (aarch64). Nothing in the
  platform cares: it is pure Python 3 standard library — no pip
  installs, no compiled extensions — so the same code runs
  unchanged. The systemd unit calls `/usr/bin/python3`, which
  Ubuntu ARM provides identically. Caddy publishes official ARM64
  builds through the same apt repo the runbook uses, so runbook
  §5 works as written.
- **Backups:** unchanged — the app backs itself up daily into
  `data/backups/` (7-day retention) and the admin UI can download
  the latest backup. On a free instance this matters *more*, not
  less: download a backup to a local machine regularly (see §7).

## 6. HTTPS before buying a domain — free subdomain route

A paid .com is deferred (Sarosh's decision: domain comes after
everything else works). Interim route, $0:

1. Create a free **DuckDNS** subdomain (e.g.
   `alphaswarm.duckdns.org` — final name is the owner's pick) and
   point it at the instance's public IP. DuckDNS updates via a
   token URL; the IP is static on OCI, so a one-time set is
   enough **[verify on duckdns.org — free tier terms]**.
   (Any equivalent free dynamic-DNS subdomain works the same way.)
2. Put that subdomain in `deploy/Caddyfile` in place of
   `alphaswarm.example.com`. Caddy obtains and auto-renews a real
   **Let's Encrypt** certificate for DuckDNS subdomains exactly as
   for a .com — visitors get normal HTTPS with no warnings.
3. Do runbook step 6 (admin signup) on this HTTPS address
   **before** sharing the link anywhere.

Later, swapping in a real .com (~$10–13/yr, see DEPLOY_PLAN.md)
is a small change: add the domain's A record → same public IP,
change one line in the Caddyfile, reload Caddy, done. No data
moves and no downtime beyond the reload — the free subdomain can
keep working in parallel during DNS propagation.

## 7. The idle-reclaim rule — read this honestly

Oracle may reclaim Always Free compute that is idle over a 7-day
window, judged on **all three**: CPU 95th percentile < 20% **and**
network < 20% **and** memory < 20% (memory applies to A1 shapes).

Why AlphaSwarm is naturally on the safer side: bot cycles run
every hour around the clock (market-data fetches = network
activity), the web app serves users, and the in-app scheduler
never sleeps. This is a genuinely always-on workload, not a parked
VM.

Honest limits: no mitigation is guaranteed. A quiet week with few
users could still look idle to Oracle's thresholds, and a
reclaimed instance is gone (data in it included) unless backups
exist outside it. Therefore:

- **Download the platform backup (.zip) from the admin UI to a
  local machine at least weekly** while on free hosting. This is
  the real insurance; everything else is hope.
- If reclamation ever happens, the exit path in §9 runs in
  reverse: new instance → restore from the downloaded backup.
- The paid VPS (DEPLOY_PLAN.md) remains the standing fallback the
  moment the platform has users who depend on it. Free hosting is
  the launch ramp, not the destination.

## 8. Cost summary — what is $0 and what could ever charge

- **$0/month, indefinitely:** the A1 instance at 2 OCPU / 12 GB,
  the 50 GB boot volume, and normal traffic — all inside the
  Always Free allowances in §1.
- **The verification hold** at signup is a temporary authorisation,
  refunded by the card issuer; it is not a fee.
- **What would create a charge:** using shapes/resources beyond
  the Always Free allowances, or spending past the 30-day trial
  credit on non-free services. The deploy in this document uses
  neither.
- **Do NOT convert the account to Pay As You Go.** Oracle offers
  the upgrade during and after signup; it is *not* required for
  Always Free, and converting turns overages into real bills.
  Decline/ignore it unless a deliberate, separate decision is
  made to start paying.

## 9. Rollback / exit — leaving Oracle (e.g. to a paid VPS)

Everything the platform owns lives in `/opt/alphaswarm/data/`
(SQLite `platform.db`, per-bot `journal.db` files, event/alert
logs, `backups/`). Exit procedure:

1. In the admin UI, **Create backup now**, then **Download latest
   backup (.zip)** — or copy the whole `data/` directory off the
   instance over SSH (`scp`/`rsync`).
2. Verify the download opens and contains `platform.db` and the
   bot journals before touching the instance.
3. Stop the instance from the OCI console (stop is enough; do not
   terminate until the new home is verified live) **[verify in
   console — stop vs terminate wording]**.
4. On the new server, follow the runbook from §1, then restore
   using runbook §9 (Restore from a backup) with the exported
   files.
5. Only once the new deployment is checked (login, bots, journals
   match): terminate the OCI instance and release its boot volume
   to free the Always Free allowance.

---

*Boundaries carried over unchanged: paper trading only, no
exchange-key collection, billing stays TEST MODE until a merchant
of record (Lemon Squeezy / Polar) is set up — none of that depends
on which server hosts the app.*
