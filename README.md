# devin_automation

Automated TODO/FIXME/HACK triage and resolution for [superset_fork](https://github.com/AshNguyen125/superset_fork) using the [Devin API](https://docs.devin.ai/api-reference/overview).

## How It Works

This project runs three complementary automations:

### Automation 1: Scanner (weekly)

Scans the superset_fork codebase for `TODO`, `FIXME`, `HACK`, and `XXX` markers, then creates a **Devin session** that reads each marker in context and produces a **ranked report** judging importance, complexity, and actionability. The report is saved to `reports/`.

```
Local grep → raw TODO list → Devin session analyzes each → ranked report (JSON + Markdown)
```

### Automation 2: Resolver (every 3 days)

Reads the latest ranked report and creates a **Devin session** that works through pending items in priority order within a **30-minute time budget**:

- **Fixable items** → Devin implements the fix and opens a PR against superset_fork
- **Complex items** → Devin creates a well-written GitHub issue with analysis and proposed approach
- **Already resolved** → Devin marks them as skipped

```
Read report → filter pending items → Devin session fixes/triages → update report with results
```

### Automation 3: Tuner (monthly, meta-automation)

The **meta-automation** closes the loop: it periodically reviews the analytics and decides whether the scanner/resolver are running too often, too rarely, or with the wrong budget. It computes deterministic candidate signals, then creates a **Devin session** that judges *why* the metrics look the way they do and proposes config changes — constrained to a legal value "ladder" and clamped to small steps. Every run appends a human-readable entry to `reports/tuning_log.md`, and when changes are warranted it opens a **`config.yaml` PR** with the reasoning so a human can review and merge (or reject).

```
Read metrics → deterministic signals → Devin session proposes {knob, old, new, rationale} → guardrails → decision log + config PR
```

The tuner adjusts: `scanner.cadence_days`, `resolver.cadence_days`, `resolver.time_budget_minutes`, `resolver.max_items`. It will not act until there is enough data (`min_scanner_runs` / `min_resolver_runs`), never moves a knob more than `max_steps_per_change` rungs at once, and respects a per-knob `cooldown_days` to prevent oscillation.

## Quick Start

### Prerequisites

- Python 3.10+
- A [Devin API key](https://docs.devin.ai/api-reference/overview) (`cog_` prefix)
- Git access to `AshNguyen125/superset_fork`

### Setup

```bash
# Clone both repos side by side
git clone https://github.com/AshNguyen125/superset_fork.git
git clone https://github.com/AshNguyen125/devin_automation.git

# You should have:
# ./superset_fork/
# ./devin_automation/

# Install dependencies
cd devin_automation
pip install -e .

# Set your Devin API key
export DEVIN_API_KEY="cog_your_api_key_here"
```

### Run the Scanner

```bash
cd devin_automation
python -m src.scanner
```

This will:
1. Grep the local `../superset_fork` checkout for TODO markers
2. Create a Devin session to analyze and rank them
3. Save the report to `reports/todo_scan_YYYY-MM-DD.json` (and `.md`)
4. Print a summary

**Options:**
```bash
# Custom repo path (if not in sibling directory)
python -m src.scanner --repo-path /path/to/superset_fork

# Custom config file
python -m src.scanner --config my_config.yaml

# Pass API key directly (instead of env var)
python -m src.scanner --api-key cog_your_key
```

### Run the Resolver

```bash
cd devin_automation
python -m src.resolver
```

This will:
1. Load the latest report from `reports/`
2. Filter to pending, actionable items
3. Create a Devin session to fix them (30-minute budget)
4. Update the report with results (PRs opened, issues created)

**Options:**
```bash
# Use a specific report (instead of latest)
python -m src.resolver --report reports/todo_scan_2026-06-10.json

# Custom config
python -m src.resolver --config my_config.yaml
```

### Example Output

After running the scanner:
```
Scanner session created: https://app.devin.ai/sessions/abc123
Waiting for Devin to analyze TODOs...

Report saved:
  JSON: reports/todo_scan_2026-06-10.json
  Markdown: reports/todo_scan_2026-06-10.md

Results: 87 TODOs analyzed
  critical: 3
  high: 12
  medium: 45
  low: 27
```

After running the resolver:
```
Found 10 pending TODOs to resolve:
  1. [critical] superset/models/core.py:1141 — use ast.literal_eval safely
  2. [high] superset/datasets/api.py:772 — narrow exception catch
  ...

Resolver session created: https://app.devin.ai/sessions/def456
Time budget: 30 minutes (wrap-up at 25 min)

Session summary: Resolved 4/10 items: 3 fixed (PRs opened), 1 issue created
  [PR]    superset/models/core.py:1141 — Replaced eval with safe parsing
         PR: https://github.com/AshNguyen125/superset_fork/pull/7
  [PR]    superset/datasets/api.py:772 — Narrowed to DatasetError
         PR: https://github.com/AshNguyen125/superset_fork/pull/8
  [ISSUE] superset/sql_lab.py:275 — Requires query builder refactor
         Issue: https://github.com/AshNguyen125/superset_fork/issues/3
```

## Configuration

Edit `config.yaml` to customize behavior:

```yaml
target_repo:
  owner: AshNguyen125
  name: superset_fork
  branch: d3-localization

scanner:
  scan_dirs:          # Directories to scan
    - superset/
    - superset-frontend/src/
  markers:            # Markers to look for
    - TODO
    - FIXME
    - HACK
    - XXX
  context_lines: 5    # Lines of context around each marker
  max_todos: 100      # Max items per scan session

resolver:
  time_budget_minutes: 5    # Wall-clock time limit (tunable)
  wrap_up_buffer_minutes: 1 # Warning before deadline
  max_items: 10             # Max items per resolver run (tunable)
  cadence_days: 3           # How often the resolver runs (tunable)

scanner:
  # ...
  cadence_days: 7           # How often the scanner runs (tunable)

tuner:
  cadence_days: 30          # How often the tuner reviews metrics
  min_scanner_runs: 3       # Min data before the tuner will act
  min_resolver_runs: 3
  cooldown_days: 30         # Don't re-change the same knob within this window
  max_steps_per_change: 1   # Max rungs a knob can move per run
  min_confidence: medium    # Apply changes at/above this confidence only
  ladders:                  # Legal values for each tunable knob
    scanner.cadence_days: [7, 14, 21, 30]
    resolver.cadence_days: [1, 2, 3, 5, 7]
    resolver.time_budget_minutes: [5, 10, 15, 20, 30, 45, 60]
    resolver.max_items: [3, 5, 10, 15, 20]
```

### Cadence is config-driven

The `cadence_days` knobs are the source of truth for run frequency. The CI workflows run on a frequent fixed cron (daily) and call a **cadence gate** (`src/gate.py`) that checks how long it's been since each automation last ran. This lets the tuner change cadence by editing `config.yaml` alone — no workflow edits needed.

```bash
# Check whether an automation is due (exit 0 = due, exit 10 = skip)
python -m src.gate scanner
python -m src.gate resolver
python -m src.gate tuner
```

## Project Structure

```
devin_automation/
├── config.yaml                 # Configuration
├── pyproject.toml              # Python project config
├── README.md
├── src/
│   ├── __init__.py
│   ├── devin_client.py         # Devin API wrapper (create session, poll, message)
│   ├── scanner.py              # Automation 1: scan & rank TODOs
│   ├── resolver.py             # Automation 2: resolve TODOs with time budget
│   ├── report_parser.py        # Parse/update TODO reports (JSON + Markdown)
│   ├── analytics.py            # Analytics dashboard (CLI + Markdown)
│   ├── tuner.py                # Automation 3: meta-automation (tune cadence/budget)
│   ├── gate.py                 # Cadence gate (decides if an automation is due)
│   └── prompts/
│       ├── scanner_prompt.md   # Prompt template for scanner sessions
│       ├── resolver_prompt.md  # Prompt template for resolver sessions
│       └── tuner_prompt.md     # Prompt template for tuner sessions
├── reports/                    # Generated reports + tuning_log.md (committed for history)
│   └── .gitkeep
└── .github/workflows/
    ├── scan_todos.yml          # Daily cron + cadence gate for scanner
    ├── resolve_todos.yml       # Daily cron + cadence gate for resolver
    └── tune_config.yml         # Weekly cron + cadence gate for tuner
```

## Analytics Dashboard

After running the scanner and/or resolver, view system effectiveness metrics:

```bash
# CLI dashboard
python -m src.analytics

# Also generate a Markdown summary file
python -m src.analytics --output-md
```

The dashboard shows:

- **Backlog**: Current TODO counts by importance, complexity, and category
- **Changes since last scan**: New vs. removed TODOs, net change
- **Resolution progress**: Cumulative fixes, issues created, fix rate (per-run and all-time)
- **System health**: Success rates, average durations, throughput (items/minute)
- **Run history**: Timeline of all scanner and resolver runs with outcomes

Example CLI output:
```
==================================================
  TODO Automation Dashboard
==================================================

Last scan:    2026-06-11  (success, 9.0m)
Last resolve: 2026-06-12  (success, 28.0m, 93.3% budget used)

--- Backlog ---
Total TODOs:    570 raw -> 100 analyzed (-13)
  Critical        2  (-1)
  High            6  (no change)
  Medium         25  (+3)
  Low            67  (-2)
  Actionable:   82.0% (82/100)

--- Resolution Progress ---
Cumulative:    12 fixed, 5 issues created, 3 skipped
PRs created:   12
Fix rate:      60.0% (last run) | 57.1% (all-time)
Backlog trend: 100 -> 88 pending (-12 over 3 report(s))

--- System Health ---
Scanner:  3/3 runs successful (100.0%)
Resolver: 2/3 runs successful (66.7%)
```

With `--output-md`, the Markdown summary is saved to `reports/analytics_summary.md` — readable directly on GitHub.

## Meta-Automation: The Tuner

The tuner reviews the analytics and proposes cadence/budget changes. Run it manually:

```bash
# Preview decisions without editing config or opening a PR (still runs a real Devin session)
python -m src.tuner --dry-run

# Apply changes to config.yaml and append to the decision log, but don't open a PR
python -m src.tuner --no-pr

# Full run: propose changes, log them, and open a config.yaml PR for human review
python -m src.tuner

# Target a specific base branch for the config PR (default: repo default branch)
python -m src.tuner --pr-base main
```

**How it decides:** the orchestrator pre-computes deterministic signals (avg new TODOs per scan, backlog direction, resolver budget utilization, fix rate, etc.) and hands them to a Devin session along with the current config and the legal ladders. The session returns one structured decision per knob (`{knob, current_value, proposed_value, action, confidence, rationale}`). Code-side **guardrails** then:

- reject any knob that isn't tunable or any value off its ladder (snapping to the nearest rung),
- clamp every change to at most `max_steps_per_change` rungs,
- reject changes below the `min_confidence` floor,
- reject changes to knobs still in `cooldown_days`,
- require a minimum data window before acting at all.

Every run writes a decision-log entry — even "no change" — to `reports/tuning_log.md` (human-readable) and `reports/tuning_log.json` (machine-readable, used for cooldown tracking). When changes survive the guardrails, the tuner opens a `config.yaml` PR whose body contains the full reasoning, so a human decides whether to apply by merging.

Example decision-log entry:
```markdown
## 2026-06-11 — Tuning Run

**Assessment**: The system is healthy on the scanning side but the resolver is severely
time-starved. 10 items in a 5-minute budget gives ~30s per item (2.9% fix rate)...

**Applied changes:**
- `resolver.time_budget_minutes`: 5 → **10** (confidence: high)
  - The resolver consistently uses 90-100% of its 5-minute budget while achieving only a
    2.9% fix rate... stepping up one rung should let fixes actually complete.
- `scanner.cadence_days`: 7 → **14** (confidence: medium)
  - Raw TODO count flat at 100 across all 3 scans; the codebase isn't accumulating debt.

**Held steady:**
- `resolver.cadence_days` stays at 3 — the poor fix rate is a time-per-item problem, not a
  cadence problem.
```

## Simulated Run (3 weeks)

To validate the system end-to-end with real data, we simulated three weeks of operation against `superset_fork` using **live Devin sessions** (not mocks). The resolver budget was capped at **5 minutes** per run to keep the simulation fast.

### Walkthrough

1. **Week 0 (scan)** — ran the scanner against `superset_fork`. It grepped ~570 raw TODO/FIXME/HACK markers, capped at 100, and a Devin session ranked them by importance, complexity, and actionability into `reports/todo_scan_*.json`.
2. **Resolver cadence (every 3 days)** — for each of the 3 weeks, the resolver picked up the latest ranked report and worked through pending items in priority order within its 5-minute budget, opening PRs / filing issues against `superset_fork` and annotating the report with statuses. Seven resolver runs total.
3. **Repeat scans (weeks 1–2)** — re-scanned to produce weekly snapshots so cross-run analytics (new vs. removed TODOs, backlog burn-down) had real history. The codebase was stable, so the scan reports were snapshotted across the three weeks.
4. **Tuner (meta-automation)** — after the simulation, ran the tuner once. It read all reports + run history via `compute_analytics`, computed deterministic signals, and a Devin session proposed config changes (see results below).

### Example Devin sessions

One representative live session for each automation:

| Automation | Example session |
|------------|-----------------|
| Scanner | https://app.devin.ai/sessions/32bad321d4c1451fb69744bacd1b6107 |
| Resolver | https://app.devin.ai/sessions/3f7505bc9c714120a3010d643a995cce |
| Tuner | https://app.devin.ai/sessions/cf8afcb6e7254a63846646c1beb42da0 |

### Results

**8 live Devin sessions** ran during the simulation (1 scanner + 7 resolver), plus 1 tuner session afterward.

```
--- Backlog (from scan) ---
Total TODOs:   100 analyzed   (2 critical, 6 high, 19 medium, 73 low)
Actionable:    79.0% (79/100)

--- Resolution (5-min budget) ---
Cumulative:    2 fixed, 2 issues created
PRs created:   2
Fix rate:      2.9% (all-time)
Backlog trend: 76 -> 75 pending (-1 over 3 reports)

--- System health ---
Scanner:   3/3 runs successful (100.0%)
Resolver:  3/7 runs successful (42.9%)
Avg resolve duration: 4.6m  (~92% of the 5-min budget used)
```

**Key takeaways:**
- The **scanner is reliable** (100% success) and the codebase debt is flat — no new TODOs accumulating.
- The **resolver was time-starved** at a 5-minute budget: ~92% budget utilization but only a 2.9% fix rate, i.e. ~30s per item — too little to complete real code fixes.
- The **tuner correctly diagnosed this** and proposed: `resolver.time_budget_minutes` 5 → 10 (high), `resolver.max_items` 10 → 5 (medium), and `scanner.cadence_days` 7 → 14 (medium), while holding `resolver.cadence_days` to isolate the budget effect. It distinguished symptom from cause — a low fix rate driven by a tight budget is a *budget* problem, not a cadence problem. The resulting config PR (with full per-knob reasoning) is the tuner's output for a human to review and merge.

## GitHub Actions (Optional)

The `.github/workflows/` directory contains cron workflows to run the automations automatically:

- **`scan_todos.yml`**: Runs daily at 9:00 UTC; the cadence gate enforces `scanner.cadence_days`
- **`resolve_todos.yml`**: Runs daily at 9:00 UTC; the cadence gate enforces `resolver.cadence_days`
- **`tune_config.yml`**: Runs weekly (Mondays); the cadence gate enforces `tuner.cadence_days`

Each workflow runs on a frequent cron and uses `src/gate.py` to decide whether enough time has passed since the last run. `workflow_dispatch` (manual trigger) bypasses the gate. To use these, add `DEVIN_API_KEY` as a repository secret in the `devin_automation` repo settings. The tuner workflow also needs `contents: write` and `pull-requests: write` permissions (already set) so it can open config PRs.

## How Devin Sessions Work

Each automation creates a [Devin session](https://docs.devin.ai/api-reference/v3/sessions/post-organizations-sessions) via the API:

1. **Scanner session** receives a list of TODO locations and analyzes each one in the context of the superset_fork codebase. It uses [structured output](https://docs.devin.ai/api-reference/overview) to return a well-typed JSON report.

2. **Resolver session** receives the ranked list and works through items. For each TODO, Devin:
   - Clones the repo and reads the surrounding code
   - Implements a fix and opens a PR, OR creates a GitHub issue with analysis
   - Gets a "wrap up" message 5 minutes before the time budget expires

The orchestrator (scanner.py / resolver.py) handles session creation, polling, time management, and report updates. The actual intelligence — reading code, judging importance, writing fixes — lives in the Devin session.

## Design Decisions

- **Why two automations?** Scanning is cheap (~5 min) and should run frequently. Fixing is expensive (~30 min) and should be controlled. Separating them lets you review the scan results before any code changes happen.
- **Why a time budget?** Prevents runaway costs. The resolver stops after 30 minutes regardless of how many items remain — they'll be picked up in the next run.
- **Why structured output?** The Devin API supports JSON Schema validation on session output. This ensures the scanner returns a well-typed report that the resolver can parse reliably.
- **Why guardrails on the tuner instead of trusting the LLM?** Deciding cadence from numbers is partly a deterministic problem, so the orchestrator constrains the session to a legal ladder, clamps step size, and enforces cooldowns. The LLM adds value where it's unique — weighing many signals at once, distinguishing symptom from cause (e.g. a low fix rate caused by a tight budget is a *budget* problem, not a cadence problem), and writing the human-readable rationale — while the code prevents runaway or oscillating changes.
- **Why does the tuner open a PR instead of editing config directly?** A config change alters how the whole system behaves, so it should get a human glance. The PR reuses the review/merge gate you already trust and never changes behavior silently.
- **Why local grep first?** Finding TODO markers is mechanical work that doesn't need an LLM. The local grep pre-filters the list so the Devin session can focus on *analysis* — reading context, judging importance, understanding code intent.
