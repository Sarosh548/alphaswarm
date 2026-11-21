# AlphaSwarm — Final Product Vision

*Written 2026-11-20. Sources for market figures: the market + GitHub
research report of 2026-11-20
(`~/workspace/research_notes/crypto-trading-bot-platforms-research-20261120-0553/report.md`).
Vendor figures are labelled as claims wherever they appear. Platform
facts are from this folder's README.md, RESULTS_PLATFORM.md,
RESULTS_ALLCOINS.md, RESULTS_HARDENING.md and DEPLOY_PLAN.md.*

## 1. The product in one paragraph

AlphaSwarm is a paper-trading platform where a team of specialist
agents trades virtual money in public, under rules anyone can inspect.
A Regime agent first identifies what kind of market each coin is in;
only the specialist strategy that has *proven itself in exactly that
kind of market* — by passing a strict walk-forward test net of fees —
is allowed to trade, and only inside a hard risk policy. Every trade
carries a plain-language reason, and every result, winners and losers
alike, is published in a continuous forward record. Users subscribe to
follow proven bots, learn how disciplined trading actually behaves,
and are actively warned away from plans and settings their account
size cannot support. AlphaSwarm never touches real money and never
holds an exchange key.

## 2. Who it serves, and what "benefit" honestly means

**Who:** retail crypto holders and curious beginners who are currently
being sold bots, signals and "AI traders" on the strength of
cherry-picked screenshots — plus more experienced users who want a
disciplined, observable paper environment before risking anything.
The research base rate is the reason this product exists: SEBI found
93% of individual F&O traders lost money over FY22–FY24, and ESMA
found 74–89% of retail CFD accounts lose money. No independent,
audited figure shows retail trading bots are profitable as a group.

**Benefit, defined honestly — four things, and profit is not one of
them:**

1. **Tools** — working multi-agent paper bots with real market data,
   a deterministic risk manager, kill switches, and per-bot journals.
2. **Discipline** — the platform enforces what most retail traders
   cannot enforce on themselves: fixed risk per trade (1%), a daily
   loss halt (3%), a maximum of 2 open positions, mandatory stop-loss
   and take-profit on every position, and sitting out when the market
   situation is unclear.
3. **Education** — every trade explains itself in plain language;
   every strategy carries a stated regime and a stated failure
   condition. Users learn what a strategy does *and when it stops
   working*, which is the lesson the rest of the market hides.
4. **Loss protection** — paper-only by construction (no real money can
   be lost here), no exchange keys to leak, budget-suitability
   warnings before a user pays for a plan their capital cannot carry,
   and a public record that shows drawdowns next to every return.

AlphaSwarm will never promise, imply or market a profit. A strategy
that performs well in paper is *evidence*, never a promise — live
markets add slippage, latency and behaviour no simulation removes.

## 3. The situation-specialist architecture, end to end

One job per agent. The pipeline, in order:

1. **Market data layer** — Binance public data (all USDT pairs for
   screening; 1h and 4h candles for analysis). Public endpoints only;
   no account, no keys.
2. **Screener agent** — filters 700+ USDT pairs down to a ranked,
   liquid shortlist (top N per bot, 5–30, default 20; tokenised
   stocks and gold excluded). Its output universe is journaled every
   cycle, and if the screener fails, the cycle fails closed — nothing
   trades on a stale universe.
3. **Regime agent** — classifies the current market situation per
   coin (e.g. trend/expansion, clean strong trend,
   compression-before-expansion, ranging/sideways, mixed/unclear).
   This is the router: its only job is identification, never trading.
4. **Specialist roster** — one strategy per situation, admitted only
   through the gate in §4:
   - *Trend / expansion* → **Breakout** (Donchian-20 breakout with a
     fail-closed 4h trend filter and a volatility guard) — the only
     strategy admitted so far.
   - *Clean strong trend* → ADX-Trend — candidate, near-miss at the
     gate (best drawdown control in testing); not admitted.
   - *Compression → expansion* → Squeeze Breakout — candidate,
     near-miss (fees consumed most of its gross edge); not admitted.
   - *Ranging / sideways* → **seat empty.** The mean-reversion
     candidate failed the gate outright. An empty seat is a feature:
     the platform says "we have nothing proven for this situation"
     instead of selling something unproven.
   - *Mixed / unclear* → **sit out.** No specialist trades. (In the
     original v1 engine, losses clustered exactly in this regime.)
5. **Analyst agents inside each specialist** — where a specialist
   uses voting (the technical engines), Trend / Momentum / Mean
   Reversion / Volatility agents vote independently and a consensus
   gate (≥3 of 4, average confidence ≥0.6) must agree before a
   proposal exists. A proposal without a stop-loss and take-profit
   is invalid by construction.
6. **News/Event agent** — polls official announcements and news
   sources, classifies events, and applies the confirmation layer:
   an event is CONFIRMED only via an official source or ≥2
   independent sources. Rumours are logged, never traded on, never
   alerted as fact.
7. **Risk Manager (deterministic, not an agent that can be argued
   with)** — ≤1% of the wallet risked per trade, 3% daily-loss halt,
   max 2 open positions per bot regardless of universe size,
   mandatory SL/TP, stale-data refusal, per-bot and global kill
   switches. No strategy output bypasses it.
8. **Paper execution + journal** — fills are simulated with a 0.1%
   fee per side charged in the journal, so published results are net
   of the same fee drag that destroys most retail strategies.
   Partial take-profit at 1R, stop-to-break-even and trailing stops
   apply in the platform engine. Every position stores its
   plain-language reason at entry.
9. **Public record** — journals feed the published bot pages: equity
   curve, win rate, profit factor, max drawdown, fees paid, and the
   full trade list with reasons — including losing trades and losing
   months. The record is continuous and cannot be backfilled; that
   is precisely what makes it worth something (§5).

## 4. The trust system

Trust is not a page on this platform; it is the mechanism of the
platform. Four load-bearing parts:

**a) The strategy admission gate.** A candidate strategy is designed
once, its parameters frozen in writing *before* evaluation, and
evaluated once — no tuning after seeing results, no second runs.
It is tested on a single shared $10,000 paper account across
BTC/ETH/SOL with 0.1%-per-side fees charged. To be admitted it must
be net-profitable after fees on the full test window **and** in both
sub-windows (first half and holdout), and no single symbol's loss
may exceed the total net profit. Research context for why the bar is
this strict: selecting the best of just 49 no-edge configurations is
expected to produce a Sharpe ratio of 1.0 by luck alone — an
undisclosed trial count makes any backtest uninterpretable. The gate
exists so AlphaSwarm's catalogue cannot be manufactured by trying
things until something looks good. Candidates that fail are
documented with their numbers, not quietly deleted.

Current state of the catalogue: **one admitted strategy.**
Breakout passed the gate under shared-account, fees-charged
accounting: +12.60% over the full 100-day window (profit factor
1.14, max drawdown 11.72%, 123 trades), positive in both sub-windows
(+4.63% and +7.96%). Its weaknesses are published with it: the edge
is thin (fees consumed roughly 70% of gross profit), one month in
the window was a losing month, and BTC lost money while ETH and SOL
carried the result. Five further strategy families have been tested
against the same gate in two research rounds; all five failed and
their results are on file. A small, defensible catalogue is the
plan — the research (§5) is explicit that an honest gate implies
exactly that.

**b) The public forward record.** Backtests are design evidence;
the forward paper record is the product's proof. The frozen
forward runners (v1 vs v2 vs Breakout, started 2026-11-19, hourly,
untouched mid-run) and the platform's live bots accumulate a record
that anyone can audit trade by trade. Losers stay in. If a strategy
decays, the record shows it and the strategy is retired publicly,
with the record intact.

**c) A plain-language reason for every trade.** Which agents voted,
what the numbers were, why the Risk Manager allowed it. No other
platform found in the research guarantees this; explanations
elsewhere are ad-hoc. Here it is a stored, contractual part of
every position.

**d) Budget-suitability guidance.** The research arithmetic is
brutal and we publish it: at an assumed 0.5% monthly net edge, a
user needs roughly **$5,800 of capital before a $29/month
subscription even breaks even** — and a $50/month plan consumes 60%
of a $1,000 account per year. AlphaSwarm therefore tells small
accounts, in the product, when a paid plan does not suit their size,
and steers them to the free tier and paper learning instead. A
platform willing to refuse revenue it would cost the user more to
earn back is claiming a trust position no incumbent occupies.

## 5. How AlphaSwarm differs from the incumbents

Per the 2026-11-20 research report (§5 verdict: the full combination
below was **not found anywhere**; each element exists somewhere, so
the moat is the combination, its enforcement, and the accumulated
record):

- **3Commas** (cloud bots via user API keys; Starter $20/$15,
  Pro $50/$38, Expert $140/$105 per month — vendor pricing verified
  live 2026-11-20): sells breadth and automation against the user's
  own exchange account. Its defining event is the category's
  defining failure: in Oct–Dec 2022 roughly **100,000 users' API
  keys leaked**; a victim group tallied over $20M in losses and one
  analysis put verified losses at $27.3M across 86 victims — via
  *trade-only* keys abused through illiquid pairs, proving
  trade-only ≠ safe. The FBI investigated; the breach was denied
  for about two months before being confirmed on 28 Dec 2022.
  **AlphaSwarm's answer is architectural: there is no key-collection
  form anywhere in the product. We cannot leak what we never hold.**
- **Cryptohopper** (Explorer $29, Adventurer $69, Hero $129 per
  month, plus paid marketplace signals and Copy Bot extras): a
  marketplace model. Marketplaces pay platforms and a few lead
  traders, not most followers — one study of 100,236 follower
  outcomes found only 48.48% positive over 90 days — and its review
  pages carry the category's standard complaint: users losing money
  on paid signals. AlphaSwarm has no marketplace and sells no
  signals; its catalogue is small because the gate is real.
- **Pionex** (exchange with 16 free built-in bots at 0.05%/trade;
  some futures bots take a 20% profit share — vendor terms): free
  and simple, and the structural price threat to every subscription
  platform. But its bots come with no validation gate, no published
  forward record, and grid's honest label — harvests chop, bleeds in
  trends — is not part of the offer. AlphaSwarm does not compete on
  bot count against free; it competes on validation, curation,
  explanation and safety, which free suites structurally do not
  provide.
- **Freqtrade** (open source, GPL-3.0, ~57k GitHub stars): honest,
  mature tooling — and DIY. The user writes, validates and operates
  everything, and Freqtrade ships **no native walk-forward module**;
  its own docs warn its optimiser overfits. Notably, no major
  open-source project ships walk-forward gating as a listing rule —
  the exact step AlphaSwarm makes mandatory is the one the ecosystem
  leaves undone. Freqtrade serves developers; AlphaSwarm serves the
  people who will never self-host.
- **Closest precedent: Keel** — publishes all 30 of its backtests
  including the 7 losers, net of costs, drawdown beside every return.
  AlphaSwarm adopts that honesty standard and extends it from
  backtests to a *continuous public forward record*, plus regime
  scoping, per-trade reasons and suitability guidance, which Keel
  does not offer.

Two caveats we accept in writing: disclosure features are copyable
once proven to sell, so the durable moat is the accumulated forward
record — months to build, impossible to backfill honestly — and
honesty constrains marketing permanently. We cannot out-promise
competitors; we can only out-prove them.

## 6. The scale path — honestly phased

**Where we are:** the current build is a *proof build* — Python
standard library only, SQLite, a single process, bot cycles
serialised under one lock, bound to localhost. It proves the
product end-to-end (accounts, plans, bots, journals, scheduler,
kill switches, backups, admin) and is entirely sufficient for the
owner, invited testers and the first public cohort. It is not a
multi-tenant SaaS architecture, and this document does not pretend
otherwise.

**Phase S1 — Public proof deployment (now).** The existing build on
a single small VPS behind HTTPS (see DEPLOY_PLAN.md: Hostinger
KVM 1, $6.49/mo promo, renews $11.99/mo; domain ~$10–13/yr; first
year ≈ $85–100). Daily backups, systemd supervision, admin-first
signup before DNS goes public. *Serves:* first hundreds of accounts,
mostly observing published bots. *Trigger to leave:* sustained
sign-ups or cycle latency making the single lock visible to users.

**Phase S2 — Real data tier.** Managed PostgreSQL replaces SQLite
(users, bots, payments, audit); per-bot journals move to the same
database with per-user row isolation; automated off-site backups
and restore drills. *Trigger:* concurrent-write contention, journal
size, or the first paying cohort — whichever comes first.

**Phase S3 — Workers and queue.** Bot cycles move out of the web
process into a job queue with separate worker processes (market
data polling, screener, regime/specialist cycles, event watcher),
so one slow or failing bot can never block the site or another
bot. The web tier becomes stateless and can run multiple replicas
behind the proxy. *Trigger:* cycle count × frequency exceeding one
process, or any cycle failure affecting unrelated users.

**Phase S4 — Scale-out.** Read replicas / caching for the public
record pages (the most-read surface), per-user rate limits,
horizontal worker scaling per market-data source, full
observability (structured logs, metrics, alerting on cycle failure
and data staleness — silent failure is the complaint users hate
most in this category), and load testing against published targets
before claiming capacity. *Trigger:* traffic, not hope — each step
is justified by a measured bottleneck from S3's metrics.

At every phase the invariants do not change: paper only, no keys,
the gate, the public record. Scale changes the plumbing, never the
promises.

## 7. Monetisation

**Tiers (live in the product today, billing in TEST MODE):**
Free — follow published bots, view the public record, paper wallet;
Pro $19/mo — news/event feed, more bots and settings; Premium
$49/mo — widest limits. Premium's auto-trading line reads **NOT YET
AVAILABLE** and stays that way: there is no real-money tier on this
roadmap (§9).

**Payment route:** Stripe is not available in Pakistan (verified
Nov 2026). Real billing, when switched on, runs through a
merchant of record — **Lemon Squeezy or Polar** — which acts as
seller, handles global tax/VAT, and pays out to a Pakistani bank
account (both list Pakistan payouts; commission ~5% plus fees, per
DEPLOY_PLAN.md). Until the owner's merchant account exists, billing
stays in clearly-labelled test mode; deployment does not wait for it.

**Honest pricing guidance (a product rule, not a footnote):** the
§4 arithmetic is shown to users at upgrade time. An account whose
size cannot plausibly cover a plan's cost at a modest assumed edge
is told so, in plain words, and pointed at Free. Longer term, the
research's own suggestion — pricing proportional to account size
rather than flat subscriptions that punish small accounts — is the
direction of travel for any tier redesign, because flat $29–$50/mo
plans on $500–$2,000 accounts are, arithmetically, a user-loss
machine.

## 8. Roadmap: today → public launch

- **2026-11-20 (today):** this vision adopted. Platform feature-
  complete for the proof stage (all-coins screener mode, Breakout
  bot, event watcher, backups, Terms/Privacy pages, fault-isolated
  scheduler, deploy package). Forward paper comparison (v1 / v2 /
  Breakout) running hourly and frozen. Strategy research rounds 1–2
  complete: 5 families tested, 0 admitted; Breakout remains the
  sole catalogue member.
- **By 2026-11-26 — the final delivery:** the complete product
  dossier compiled from on-disk records — platform status, the full
  test dossier (every RESULTS file), the honest forward scorecard,
  the situation-specialist map, budget guidance, and the remaining
  launch steps below. Nothing in it is a projection; it is a report
  on what exists.
- **Owner's steps (only the owner can do these):** buy the VPS and
  domain (his card, ~$85–100 for the first year); create the Lemon
  Squeezy or Polar account (CNIC + Pakistani bank) when real
  subscriptions are to be switched on; hand over VPS access via
  secure entry (never a password in chat); and be the **first
  signup** on the production instance — first signup becomes admin,
  so this happens before DNS goes public.
- **Deploy day (Muse, same day the VPS lands):** app + systemd +
  Caddy HTTPS, firewall, daily backups, admin account created,
  end-to-end test (signup → bot cycle → test checkout), live link
  to the owner.
- **First public weeks:** paper record accumulates in public;
  subscriptions stay test-mode until the owner reviews the record
  and the merchant account is live. Invited users first, public
  signup after the record has something honest to show.
- **After launch:** strategy research continues under the same gate
  (new candidates judged on data that arrives *after* their design,
  to avoid process-level overfitting on the original 100 days); the
  empty ranging/sideways seat is the first research gap to attack.
  Scale phases S2–S4 proceed on measured triggers (§6).

## 9. Non-goals — explicit, permanent

- **No real-money execution.** No live order placement, no live
  pilot, on any tier, at any price.
- **No exchange-key custody.** No form collects API keys; there is
  nothing to leak, sell or abuse.
- **No guaranteed or implied returns.** No "profitable every month"
  language anywhere — in product copy, marketing or conversation.
  Results are published with drawdowns and losing periods attached.
- **No paid signals.** Nothing on the platform tells a user what to
  buy with real money.
- **No strategy without the gate.** No exceptions for demand, hype
  or a good-looking backtest screenshot — including strategies that
  are popular elsewhere; no shared or open-source strategy has a
  verified live record, so importing one imports unproven risk,
  not an edge.
- **No catalogue breadth for its own sake.** An empty seat is an
  honest answer. The roster grows at the speed of proof, not at
  the speed of marketing.
