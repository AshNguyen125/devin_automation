"""Simulate 3 weeks of scanner + resolver runs.

Runs 1 real scanner session, creates 3 weekly snapshots,
then runs 7 real resolver sessions (5-min budget each).
"""

from __future__ import annotations

import copy
import json
import logging
import os
import shutil
import sys
import time
from dataclasses import asdict
from pathlib import Path

import yaml

from src.devin_client import DevinClient
from src.report_parser import (
    OUTCOME_FAILURE,
    OUTCOME_PARTIAL,
    OUTCOME_SUCCESS,
    RunMetadata,
    TodoItem,
    TodoReport,
    get_pending_todos,
    load_report,
    now_iso,
    save_report,
    update_report,
)
from src.resolver import RESOLVER_OUTPUT_SCHEMA, build_resolver_prompt, _apply_results_to_report
from src.scanner import SCANNER_OUTPUT_SCHEMA, collect_todos, build_prompt, _build_report_from_output

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Simulated dates for 3 weeks
WEEK_DATES = [
    "2026-05-26",  # Week 1 Monday
    "2026-06-02",  # Week 2 Monday
    "2026-06-09",  # Week 3 Monday
]

# Resolver trigger dates (every 3 days)
RESOLVER_DATES = [
    ("2026-05-28", "2026-05-26"),  # day 3 → uses week-1 report
    ("2026-05-31", "2026-05-26"),  # day 6 → uses week-1 report
    ("2026-06-04", "2026-06-02"),  # day 9 → uses week-2 report
    ("2026-06-07", "2026-06-02"),  # day 12 → uses week-2 report
    ("2026-06-10", "2026-06-09"),  # day 15 → uses week-3 report
    ("2026-06-13", "2026-06-09"),  # day 18 → uses week-3 report
    ("2026-06-16", "2026-06-09"),  # day 21 → uses week-3 report
]


def run_scanner_session(config: dict, repo_path: Path, api_key: str) -> dict | None:
    """Run a single scanner session and return the structured output."""
    todos = collect_todos(repo_path, config)
    if not todos:
        return None

    prompt = build_prompt(todos, config)
    client = DevinClient(api_key=api_key, org_id=config["devin"]["org_id"])
    repo_url = f"github.com/{config['target_repo']['owner']}/{config['target_repo']['name']}"

    session = client.create_session(
        prompt=prompt,
        repos=[repo_url],
        title=f"TODO Scanner — {config['target_repo']['name']} (simulation)",
        tags=["automation", "todo-scanner", "simulation"],
        structured_output_schema=SCANNER_OUTPUT_SCHEMA,
        structured_output_required=True,
    )
    print(f"  Scanner session: {session.url}")

    result = client.poll_until_done(session.session_id, timeout_seconds=1800)

    return {
        "structured_output": result.structured_output,
        "session_id": result.session_id,
        "url": result.url,
        "status": result.status,
        "status_detail": result.status_detail or "",
    }


def create_weekly_report(
    scan_date: str,
    config: dict,
    structured_output: dict | None,
    raw_todos: list[dict],
    scanner_result: dict,
    duration: float,
    output_dir: Path,
    previous_report: TodoReport | None = None,
) -> TodoReport:
    """Create a scanner report for a simulated week."""
    report = _build_report_from_output(structured_output, config, raw_todos)
    report.scan_date = f"{scan_date}T09:00:00+00:00"

    # If we have a previous report, carry over resolution statuses
    if previous_report:
        prev_status_map: dict[str, TodoItem] = {}
        for t in previous_report.todos:
            prev_status_map[f"{t.file}:{t.line}"] = t

        for t in report.todos:
            key = f"{t.file}:{t.line}"
            if key in prev_status_map:
                prev = prev_status_map[key]
                t.status = prev.status
                t.resolution_note = prev.resolution_note
                t.pr_url = prev.pr_url
                t.issue_url = prev.issue_url

    # Add scanner run metadata
    meta = RunMetadata(
        automation="scanner",
        run_id=f"scan-{scan_date}T09:00:00+00:00",
        started_at=f"{scan_date}T09:00:00+00:00",
        finished_at=f"{scan_date}T09:{int(duration // 60):02d}:{int(duration % 60):02d}+00:00",
        duration_seconds=round(duration, 1),
        devin_session_id=scanner_result["session_id"],
        devin_session_url=scanner_result["url"],
        devin_session_status=scanner_result["status"],
        devin_session_status_detail=scanner_result["status_detail"],
        outcome=OUTCOME_SUCCESS if structured_output else OUTCOME_FAILURE,
        raw_todos_found=len(raw_todos),
        todos_sent_to_devin=min(len(raw_todos), config["scanner"]["max_todos"]),
        todos_analyzed=len(report.todos),
    )
    report.run_history.append(meta)

    # Save with the simulated date
    json_path = output_dir / f"todo_scan_{scan_date}.json"
    md_path = output_dir / f"todo_scan_{scan_date}.md"
    json_path.write_text(json.dumps(asdict(report), indent=2) + "\n")

    return report


def run_resolver_session(
    config: dict,
    report: TodoReport,
    api_key: str,
    resolver_date: str,
    budget_minutes: int = 5,
    wrap_up_buffer: int = 1,
) -> dict:
    """Run a single resolver session and return results."""
    pending = get_pending_todos(report)
    max_items = config["resolver"].get("max_items", 10)
    if len(pending) > max_items:
        pending = pending[:max_items]

    if not pending:
        print(f"  [{resolver_date}] No pending items to resolve")
        return {"structured_output": None, "items": 0}

    print(f"  [{resolver_date}] {len(pending)} pending items to resolve")

    prompt = build_resolver_prompt(pending, config)
    client = DevinClient(api_key=api_key, org_id=config["devin"]["org_id"])
    repo_url = f"github.com/{config['target_repo']['owner']}/{config['target_repo']['name']}"

    session = client.create_session(
        prompt=prompt,
        repos=[repo_url],
        title=f"TODO Resolver — simulation ({resolver_date})",
        tags=["automation", "todo-resolver", "simulation"],
        structured_output_schema=RESOLVER_OUTPUT_SCHEMA,
        structured_output_required=True,
    )
    print(f"  Resolver session: {session.url}")

    start = time.monotonic()
    result = client.poll_with_budget(
        session.session_id,
        budget_minutes=budget_minutes,
        wrap_up_buffer_minutes=wrap_up_buffer,
    )
    duration = time.monotonic() - start
    budget_used = min(duration / 60, budget_minutes)

    # Apply results
    _apply_results_to_report(report, result.structured_output)

    # Count outcomes
    resolved_items = (result.structured_output or {}).get("resolved_items", [])
    items_fixed = sum(1 for r in resolved_items if r["action"] == "fixed")
    items_issue = sum(1 for r in resolved_items if r["action"] == "issue_created")
    items_skipped = sum(1 for r in resolved_items if r["action"] == "skipped")
    items_not_reached = sum(1 for r in resolved_items if r["action"] == "not_reached")

    if result.status == "error":
        outcome = OUTCOME_FAILURE
    elif resolved_items and (items_fixed > 0 or items_issue > 0):
        outcome = OUTCOME_SUCCESS
    elif resolved_items:
        outcome = OUTCOME_PARTIAL
    else:
        outcome = OUTCOME_FAILURE

    meta = RunMetadata(
        automation="resolver",
        run_id=f"resolve-{resolver_date}T09:00:00+00:00",
        started_at=f"{resolver_date}T09:00:00+00:00",
        finished_at=f"{resolver_date}T09:{int(duration // 60):02d}:{int(duration % 60):02d}+00:00",
        duration_seconds=round(duration, 1),
        devin_session_id=result.session_id,
        devin_session_url=result.url,
        devin_session_status=result.status,
        devin_session_status_detail=result.status_detail or "",
        outcome=outcome,
        budget_minutes=budget_minutes,
        budget_used_minutes=round(budget_used, 1),
        budget_utilization_pct=round((budget_used / budget_minutes) * 100, 1),
        items_attempted=len(pending),
        items_fixed=items_fixed,
        items_issue_created=items_issue,
        items_skipped=items_skipped,
        items_not_reached=items_not_reached,
        prs_created=len(result.pull_requests),
        issues_created=items_issue,
    )
    report.run_history.append(meta)

    print(
        f"  Result: {outcome} | fixed={items_fixed} issues={items_issue} "
        f"skipped={items_skipped} not_reached={items_not_reached} | "
        f"{budget_used:.1f}/{budget_minutes}min"
    )

    return {
        "structured_output": result.structured_output,
        "items": len(pending),
        "outcome": outcome,
    }


def main() -> None:
    config_path = Path("config.yaml")
    with open(config_path) as f:
        config = yaml.safe_load(f)

    repo_path = Path("../superset_fork")
    if not repo_path.exists():
        print("Error: superset_fork not found at ../superset_fork")
        sys.exit(1)

    api_key = os.environ.get("DEVIN_API_KEY", "")
    if not api_key:
        print("Error: DEVIN_API_KEY not set")
        sys.exit(1)

    output_dir = Path(config["reports"]["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)

    # Clean old reports
    for f in output_dir.glob("todo_scan_*.json"):
        f.unlink()
    for f in output_dir.glob("todo_scan_*.md"):
        f.unlink()
    for f in output_dir.glob("analytics_summary.md"):
        f.unlink()

    print("=" * 60)
    print("  3-Week Simulation: Scanner + Resolver")
    print("  Budget: 5 min per resolver run")
    print("=" * 60)

    # ---- Step 1: Run scanner once (real session) ----
    print("\n[STEP 1] Running scanner (real Devin session)...")
    raw_todos = collect_todos(repo_path, config)
    print(f"  Found {len(raw_todos)} raw TODOs")

    scan_start = time.monotonic()
    scanner_result = run_scanner_session(config, repo_path, api_key)
    scan_duration = time.monotonic() - scan_start

    if not scanner_result or not scanner_result["structured_output"]:
        print("  ERROR: Scanner session failed to produce output")
        sys.exit(1)

    print(f"  Scanner completed in {scan_duration:.0f}s")

    # ---- Step 2: Create weekly reports ----
    print("\n[STEP 2] Creating 3 weekly report snapshots...")
    reports: dict[str, TodoReport] = {}
    prev_report = None

    for i, week_date in enumerate(WEEK_DATES):
        report = create_weekly_report(
            scan_date=week_date,
            config=config,
            structured_output=scanner_result["structured_output"],
            raw_todos=raw_todos,
            scanner_result=scanner_result,
            duration=scan_duration,
            output_dir=output_dir,
            previous_report=prev_report,
        )
        reports[week_date] = report
        print(f"  Week {i+1} report: {week_date} ({len(report.todos)} items, "
              f"{len(get_pending_todos(report))} pending)")

    # ---- Step 3: Run resolvers ----
    print("\n[STEP 3] Running resolver sessions (7 runs, 5-min budget each)...")
    for i, (resolve_date, report_date) in enumerate(RESOLVER_DATES, 1):
        report = reports[report_date]
        print(f"\n--- Resolver run {i}/7 (simulated: {resolve_date}) ---")
        run_resolver_session(
            config=config,
            report=report,
            api_key=api_key,
            resolver_date=resolve_date,
            budget_minutes=5,
            wrap_up_buffer=1,
        )

        # Save updated report
        json_path = output_dir / f"todo_scan_{report_date}.json"
        json_path.write_text(json.dumps(asdict(report), indent=2) + "\n")

        # If this resolver updates a week's report, carry statuses forward
        # to subsequent weeks
        week_idx = WEEK_DATES.index(report_date)
        if week_idx < len(WEEK_DATES) - 1:
            next_week = WEEK_DATES[week_idx + 1]
            next_report = reports[next_week]
            # Carry over resolution statuses
            status_map = {f"{t.file}:{t.line}": t for t in report.todos}
            for t in next_report.todos:
                key = f"{t.file}:{t.line}"
                if key in status_map:
                    src = status_map[key]
                    t.status = src.status
                    t.resolution_note = src.resolution_note
                    t.pr_url = src.pr_url
                    t.issue_url = src.issue_url

    # ---- Step 4: Save all final reports with Markdown ----
    print("\n[STEP 4] Saving final reports...")
    for week_date, report in reports.items():
        json_path = output_dir / f"todo_scan_{week_date}.json"
        md_path = output_dir / f"todo_scan_{week_date}.md"
        json_path.write_text(json.dumps(asdict(report), indent=2) + "\n")
        from src.report_parser import _render_markdown
        md_path.write_text(_render_markdown(report))
        pending = len(get_pending_todos(report))
        total_runs = len(report.run_history)
        print(f"  {week_date}: {len(report.todos)} items, {pending} pending, {total_runs} runs")

    # ---- Step 5: Run analytics ----
    print("\n[STEP 5] Running analytics...")
    from src.analytics import load_all_reports, compute_analytics, render_cli, render_markdown

    all_reports = load_all_reports(output_dir)
    summary = compute_analytics(all_reports)

    cli_output = render_cli(summary)
    print("\n" + cli_output)

    # Save Markdown summary
    md_path = output_dir / "analytics_summary.md"
    md_path.write_text(render_markdown(summary))
    print(f"Markdown summary: {md_path}")

    print("\n" + "=" * 60)
    print("  Simulation complete!")
    print("=" * 60)


if __name__ == "__main__":
    main()
