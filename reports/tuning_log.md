# Tuning Decision Log

Chronological log of cadence/budget tuning decisions made by the meta-automation (`src/tuner.py`). Newest entries at the bottom.

## 2026-06-12 — Tuning Run

**Devin session**: https://app.devin.ai/sessions/41299c979be543c883fbdc3b1b538515

**Assessment**: The scanner is healthy and over-polling a stable codebase — slow it down to save cost. The resolver is severely time-starved: 91.7% average budget utilization with only a 2.9% fix rate and 57% failure/partial rate clearly indicates items are being cut off before completion. The fix is two-pronged: increase the time budget from 5 to 10 minutes AND reduce max_items from 10 to 5, giving each item ~2 minutes instead of ~30 seconds. This should significantly improve fix rate and success rate. Hold resolver cadence steady at 3 days to preserve throughput and allow clean measurement of whether the budget/items changes are effective.

**Applied changes:**

- `scanner.cadence_days`: 7 → **14** (confidence: medium)
  - Raw TODO count is completely flat at 100 across all 3 scans (avg delta = 0.0) and the backlog has barely moved (76 → 75). The codebase is stable with no new TODOs being introduced. Scanning every 7 days is wasteful; moving to 14 days halves the cost while still catching any future uptick within two weeks.
- `resolver.time_budget_minutes`: 5 → **10** (confidence: high)
  - This is the primary bottleneck. The resolver averages 91.7% budget utilization across all 7 runs (4.6 min of 5 min), hit 100% ceiling on one run, and achieved only a 2.9% fix rate. With 10 items attempted per run, each item gets roughly 30 seconds — far too little for meaningful code fixes involving context understanding and testing. Stepping the budget up one rung to 10 minutes doubles available time and should materially improve the fix rate. The failure and partial outcomes (4 of 7 runs) are consistent with time starvation, not inherently unfixable items.
- `resolver.max_items`: 10 → **5** (confidence: medium)
  - Every run attempts 10 items but fixes 0–1. The resolver is spreading itself too thin. Reducing to 5 items, combined with the budget increase to 10 minutes, gives ~2 minutes per item instead of ~30 seconds — a 4x improvement in time per item. This focuses effort on fewer items with a much higher chance of completion. The current pattern of attempting 10 and fixing 0 is worse than attempting 5 with a realistic chance of fixing 2–3.

**Held steady:**

- `resolver.cadence_days` stays at 3 — The resolver's low fix rate (2.9%) and high failure/partial rate (57.1%) are caused by insufficient time per item, not by running too frequently. Slowing the cadence would not address the root cause and would reduce throughput once the budget constraint is relieved. Keeping cadence at 3 days lets us observe whether the budget increase improves fix rate before making further adjustments.

