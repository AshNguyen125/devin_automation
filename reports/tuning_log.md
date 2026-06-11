# Tuning Decision Log

Chronological log of cadence/budget tuning decisions made by the meta-automation (`src/tuner.py`). Newest entries at the bottom.

## 2026-06-11 — Tuning Run

**Devin session**: https://app.devin.ai/sessions/e1b8e85ce40a4264b03b7c18d6392520

**Assessment**: The scanner side is healthy and the codebase is stable — slow it to 14-day cadence to save cost. The resolver's core problem is budget starvation: 91.7% average utilization, 2.9% fix rate, and only ~30 seconds per item. The single highest-impact change is doubling the time budget from 5 to 10 minutes. Hold resolver cadence and max_items steady to isolate the effect of the budget increase. If the fix rate doesn't improve after 3-4 runs at 10 minutes, the next step would be reducing max_items from 10 to 5.

**Applied changes:**

- `scanner.cadence_days`: 7 → **14** (confidence: medium)
  - Raw TODO count has been perfectly stable at 100 across all 3 scans (avg delta = 0.0), and the pending backlog barely moved (76→76→75). No new TODOs are being introduced into the codebase. Scanning every 14 days instead of 7 halves the scanning cost without losing meaningful signal in a demonstrably stable codebase. Only medium confidence because we have just 3 data points — if new development picks up, we may need to revert.
- `resolver.time_budget_minutes`: 5 → **10** (confidence: high)
  - This is the clearest signal in the data. Budget utilization averages 91.7% with one run hitting the 100% ceiling. Across 7 runs and 70 attempted items, only 2 were fixed (2.9%), strongly suggesting items are being abandoned mid-fix due to time pressure. At 5 min / 10 items, each item gets ~30 seconds — far too little for meaningful code changes. Stepping up to 10 minutes doubles per-item time to ~1 minute, which should materially improve the fix rate. Every run is consuming nearly the full budget, so the additional time will be utilized, not wasted.

**Held steady:**

- `resolver.cadence_days` stays at 3 — The resolver's poor 42.9% success rate and 2.9% fix rate are symptoms of budget starvation, not excessive cadence. With 91.7% average budget utilization and only ~30 seconds per item (5 min / 10 items), the resolver simply runs out of time before it can complete fixes. Reducing cadence would mask the root cause; increasing cadence without fixing the budget would waste more runs. Hold cadence steady and address the budget first.
- `resolver.max_items` stays at 10 — Reducing max_items from 10 to 5 would also increase per-item time, but changing both max_items and budget simultaneously would make it impossible to isolate which lever improved the fix rate. The budget increase alone doubles per-item time from ~30s to ~1min. If the fix rate doesn't improve after a few runs at 10 minutes, reducing max_items to 5 would be the natural next step. Holding steady avoids over-correction.

