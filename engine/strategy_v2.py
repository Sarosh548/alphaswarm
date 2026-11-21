"""Strategy v2 — a clearly separated variant of v1, designed ONCE from
DIAGNOSIS.md and evaluated ONCE (walk-forward, see RESULTS_PHASE2.md).
v1 code (agents.py, decision.py, policy.py) is untouched; v2 is only
this filter, applied to a v1 proposal before the risk-policy gate.

Exactly two changes vs v1, each justified in DIAGNOSIS.md:

Change A — hard higher-timeframe trend filter (DIAGNOSIS section 5).
  A proposal is valid only in the direction of the real 4h trend
  (EMA50 vs EMA200 on 4h candles closed before the decision). An
  "unknown" 4h trend blocks the proposal (fail closed). For this to
  work the snapshot must carry the real 4h label — the live engine
  already supplies it, and the v2 backtest walk sets it too (in v1's
  Phase 1 backtest definition the snapshot carried "unknown"). A side
  effect, disclosed in the diagnosis: TrendAgent's pre-existing HTF
  conviction-halving becomes active under v2.

Change B — Momentum agreement required (DIAGNOSIS section 2).
  A proposal is invalid unless MomentumAgent is one of the agreeing
  agents for its direction. In the v1 diagnosis, proposals formed
  without momentum agreement went 0-for-8 (-785.91 USDT) and
  MeanReversion-agreed proposals 0-for-9 (-885.91), in both halves
  of the data.

Deliberately NOT changed (see DIAGNOSIS section 6): SL/TP geometry
(exonerated), the symbol list, every threshold and parameter, and no
efficiency-ratio regime gate (benefit was window-confined and its
threshold would be fitted to the diagnosis data).
"""


def filter_proposal(proposal, snap, votes):
    """Return (ok, reason). reason is a short machine-readable string
    when the proposal is blocked, else None."""
    # Change B first (cheap, structural): momentum must confirm.
    if "MomentumAgent" not in proposal.get("agreeing_agents", []):
        return False, "v2_momentum_required"
    # Change A: trade only with the 4h trend; unknown blocks.
    htf = snap.get("htf_trend", "unknown")
    if htf not in ("up", "down"):
        return False, "v2_htf_unknown"
    if (htf == "up") != (proposal.get("direction") == "long"):
        return False, "v2_htf_filter"
    return True, None
