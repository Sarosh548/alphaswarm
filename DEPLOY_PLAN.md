# AlphaSwarm — Asal Hosting Deploy Plan
Prepared 2026-11-19. Platform: ~/workspace/alphaswarm-platform/ (Python stdlib app, SQLite, port 8000, currently local-only at 127.0.0.1).

## Recommendation (saral aur sasta rasta)
- **VPS:** Hostinger KVM 1 — 1 vCPU / 4 GB RAM / 50 GB NVMe, $6.49/mo promo (24-month upfront ~$156, renews $11.99/mo). Pakistan mein aam, card se payment asan. Alternatives: Hetzner CX23 €5.99/mo (~$7, Germany/Finland, kabhi out of stock), OVHcloud VPS-1 $4.54/mo (12-month term; Singapore/Mumbai region PK users ke qareeb), DigitalOcean $6–12/mo (asan console, mehenga).
- **Location:** Singapore ya Mumbai sab se qareeb; EU bhi chalega — bot hourly cycles par hai, latency farq nahi padta.
- **Domain:** .com, Cloudflare Registrar ya Porkbun se ~$10–13/year (at-cost pricing).
- **Total pehla saal:** takreeban $85–100 ≈ **Rs 24,000–28,000** (rate ~Rs 276.5/$). Monthly running: ~Rs 1,800–2,000.

## Technical setup (deploy ke din, Muse karega)
1. Ubuntu VPS par app copy, systemd service (auto-restart, boot par start).
2. Caddy reverse proxy — automatic HTTPS (Let's Encrypt), domain → app.
3. Firewall: sirf 80/443/SSH khule; app port bahar se band.
4. Daily SQLite backups (bots ke journals + users DB), 7 din retention.
5. **Admin pehle, public baad mein:** DNS public karne se pehle Sarosh ka admin account banega (first-signup = admin hai, is liye ye tartib lazmi hai), phir signup public khulega.

## Payments — sab se zaroori sach
- **Stripe Pakistan mein available NAHI hai** (Stripe ki apni country list se verified, Nov 2026). Pakistani business/CNIC/bank se Stripe account nahi khulta. US LLC + Stripe Atlas wala rasta ~$500+ aur bhaari hai — abhi nahi.
- **Sahi rasta: Merchant of Record** — Lemon Squeezy ya Polar. Ye khud seller ban kar duniya se card lete hain, tax/VAT khud sambhalte hain, aur **Pakistan ke bank mein payout dete hain** (dono ki supported-countries list mein Pakistan hai, Oct–Nov 2026 checks). Commission ~5% + fee.
- Jab tak MoR account nahi banta, platform test-mode billing par rahega — deploy is se nahi rukta.

## Legal / safety basics
- Platform paper-trading only hai — yehi is ki hifazat hai. Har pricing/performance page par risk disclaimer pehle se hai.
- Live trading / user exchange keys wala step is plan mein NAHI hai — wo alag faisla + legal review ke baghair nahi hoga.
- Terms + Privacy pages launch se pehle add honge (Muse draft karega).

## Sarosh ke kaam (sirf ye, baqi sab Muse ka)
1. VPS khareedna (apne card se) — Hostinger KVM 1 recommend.
2. Domain khareedna (alphaswarm jaisa naam check karke — final naam wo confirm karega).
3. Lemon Squeezy account banana (CNIC + Pakistani bank) — jab subscriptions asal on karni hon; deploy ke liye lazmi nahi.
4. VPS ke login details secure tareeqay se dena (chat mein password NAHI — secure entry).

## Timeline
- VPS + domain milte hi **usi din** deploy: setup, HTTPS, admin account, test (signup → bot cycle → subscribe test flow) — phir live link Sarosh ko.
- Pehle 2 haftay: sirf paper record banta rahega; subscriptions asal on karna paper record ke review ke baad (roadmap gate).

## Risks / notes
- Single-process app hai — shuru ke users ke liye kaafi; baray traffic par pehle metrics, phir upgrade (Postgres/queue) ka faisla.
- Backups ke baghair koi data mehfooz nahi samjha jaye — is liye step 4 lazmi hai.
- Prices Nov 2026 ki web checks par hain; khareed ke waqt final price dobara verify hoga.
