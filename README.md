# devin_automation

Automated TODO/FIXME/HACK triage and resolution for [superset_fork](https://github.com/AshNguyen125/superset_fork) using the [Devin API](https://docs.devin.ai/api-reference/overview).

## How It Works

This project runs two complementary automations:

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
  time_budget_minutes: 30   # Wall-clock time limit
  wrap_up_buffer_minutes: 5 # Warning before deadline
  max_items: 10             # Max items per resolver run
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
│   └── prompts/
│       ├── scanner_prompt.md   # Prompt template for scanner sessions
│       └── resolver_prompt.md  # Prompt template for resolver sessions
├── reports/                    # Generated reports (committed for history)
│   └── .gitkeep
└── .github/workflows/
    ├── scan_todos.yml          # Weekly cron for scanner
    └── resolve_todos.yml       # Every-3-days cron for resolver
```

## GitHub Actions (Optional)

The `.github/workflows/` directory contains cron workflows to run the automations automatically:

- **`scan_todos.yml`**: Runs the scanner every Monday at 9:00 UTC
- **`resolve_todos.yml`**: Runs the resolver every 3 days at 9:00 UTC

To use these, add `DEVIN_API_KEY` as a repository secret in the `devin_automation` repo settings.

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
- **Why local grep first?** Finding TODO markers is mechanical work that doesn't need an LLM. The local grep pre-filters the list so the Devin session can focus on *analysis* — reading context, judging importance, understanding code intent.
