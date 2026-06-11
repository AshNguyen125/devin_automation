"""Analytics dashboard for TODO scanner and resolver automation.

Reads all reports in reports/ and computes per-run and cross-run metrics.
Outputs a CLI dashboard and optionally a Markdown summary file.

Usage:
    python -m src.analytics [--config config.yaml] [--output-md]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

from src.report_parser import (
    OUTCOME_SUCCESS,
    RunMetadata,
    TodoItem,
    TodoReport,
    load_report,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

IMPORTANCE_ORDER = ["critical", "high", "medium", "low"]


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class DebtProfile:
    """Snapshot of the TODO debt at a point in time."""

    total_raw: int = 0
    total_analyzed: int = 0
    by_importance: dict[str, int] = field(default_factory=dict)
    by_complexity: dict[str, int] = field(default_factory=dict)
    by_category: dict[str, int] = field(default_factory=dict)
    actionable_count: int = 0
    actionable_pct: float = 0


@dataclass
class DiffStats:
    """Changes between two consecutive scanner reports."""

    new_items: list[str] = field(default_factory=list)  # file:line keys
    removed_items: list[str] = field(default_factory=list)
    net_change: int = 0


@dataclass
class AnalyticsSummary:
    """Full analytics across all reports."""

    # Scanner metrics
    scanner_runs: list[RunMetadata] = field(default_factory=list)
    scanner_success_rate: float = 0
    latest_debt: DebtProfile | None = None
    debt_trend: list[tuple[str, int]] = field(default_factory=list)  # (date, count)
    critical_high_trend: list[tuple[str, int]] = field(default_factory=list)
    diff_from_previous: DiffStats | None = None

    # Resolver metrics
    resolver_runs: list[RunMetadata] = field(default_factory=list)
    resolver_success_rate: float = 0
    cumulative_fixed: int = 0
    cumulative_issues: int = 0
    cumulative_skipped: int = 0
    cumulative_prs: int = 0
    all_time_fix_rate: float = 0
    backlog_trend: list[tuple[str, int]] = field(default_factory=list)  # (date, pending)

    # Reports
    reports: list[TodoReport] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Computation
# ---------------------------------------------------------------------------


def load_all_reports(output_dir: str | Path) -> list[TodoReport]:
    """Load all scanner reports from the output directory, sorted chronologically."""
    output_dir = Path(output_dir)
    json_files = sorted(output_dir.glob("todo_scan_*.json"))
    reports = []
    for f in json_files:
        try:
            reports.append(load_report(f))
        except (json.JSONDecodeError, TypeError, KeyError) as exc:
            logger.warning("Skipping corrupt report %s: %s", f, exc)
    return reports


def compute_debt_profile(report: TodoReport) -> DebtProfile:
    """Compute the debt profile from a single report."""
    todos = report.todos
    total = len(todos)
    if total == 0:
        return DebtProfile()

    by_imp = Counter(t.importance for t in todos)
    by_cplx = Counter(t.complexity for t in todos)
    by_cat = Counter(t.category for t in todos)
    actionable = sum(1 for t in todos if t.actionable)

    # Get raw count from scanner run metadata if available
    scanner_runs = [r for r in report.run_history if r.automation == "scanner"]
    raw_count = scanner_runs[-1].raw_todos_found if scanner_runs else total

    return DebtProfile(
        total_raw=raw_count,
        total_analyzed=total,
        by_importance=dict(by_imp),
        by_complexity=dict(by_cplx),
        by_category=dict(by_cat),
        actionable_count=actionable,
        actionable_pct=round((actionable / total) * 100, 1) if total else 0,
    )


def compute_diff(prev: TodoReport, curr: TodoReport) -> DiffStats:
    """Compute new and removed items between two reports."""
    prev_keys = {f"{t.file}:{t.line}" for t in prev.todos}
    curr_keys = {f"{t.file}:{t.line}" for t in curr.todos}

    new = sorted(curr_keys - prev_keys)
    removed = sorted(prev_keys - curr_keys)

    return DiffStats(
        new_items=new,
        removed_items=removed,
        net_change=len(new) - len(removed),
    )


def compute_analytics(reports: list[TodoReport]) -> AnalyticsSummary:
    """Compute full analytics from all reports."""
    summary = AnalyticsSummary(reports=reports)

    # Collect all run metadata across reports
    all_runs: list[RunMetadata] = []
    for report in reports:
        all_runs.extend(report.run_history)

    summary.scanner_runs = [r for r in all_runs if r.automation == "scanner"]
    summary.resolver_runs = [r for r in all_runs if r.automation == "resolver"]

    # Scanner success rate
    if summary.scanner_runs:
        successes = sum(1 for r in summary.scanner_runs if r.outcome == OUTCOME_SUCCESS)
        summary.scanner_success_rate = round(
            (successes / len(summary.scanner_runs)) * 100, 1
        )

    # Resolver success rate and cumulative counts
    if summary.resolver_runs:
        successes = sum(1 for r in summary.resolver_runs if r.outcome == OUTCOME_SUCCESS)
        summary.resolver_success_rate = round(
            (successes / len(summary.resolver_runs)) * 100, 1
        )
        summary.cumulative_fixed = sum(r.items_fixed for r in summary.resolver_runs)
        summary.cumulative_issues = sum(r.items_issue_created for r in summary.resolver_runs)
        summary.cumulative_skipped = sum(r.items_skipped for r in summary.resolver_runs)
        summary.cumulative_prs = sum(r.prs_created for r in summary.resolver_runs)

        total_attempted = sum(r.items_attempted for r in summary.resolver_runs)
        if total_attempted > 0:
            summary.all_time_fix_rate = round(
                (summary.cumulative_fixed / total_attempted) * 100, 1
            )

    # Debt trends (from scanner reports)
    for report in reports:
        date_slug = report.scan_date[:10]
        profile = compute_debt_profile(report)
        summary.debt_trend.append((date_slug, profile.total_raw))
        crit_high = profile.by_importance.get("critical", 0) + profile.by_importance.get(
            "high", 0
        )
        summary.critical_high_trend.append((date_slug, crit_high))

    # Latest debt profile
    if reports:
        summary.latest_debt = compute_debt_profile(reports[-1])

    # Diff from previous
    if len(reports) >= 2:
        summary.diff_from_previous = compute_diff(reports[-2], reports[-1])

    # Backlog trend (pending items per report)
    for report in reports:
        date_slug = report.scan_date[:10]
        pending = sum(1 for t in report.todos if t.status == "PENDING" and t.actionable)
        summary.backlog_trend.append((date_slug, pending))

    return summary


# ---------------------------------------------------------------------------
# CLI output
# ---------------------------------------------------------------------------


def _trend_arrow(values: list[tuple[str, int]]) -> str:
    """Return a trend indicator based on last two values."""
    if len(values) < 2:
        return ""
    prev, curr = values[-2][1], values[-1][1]
    diff = curr - prev
    if diff > 0:
        return f" (+{diff})"
    elif diff < 0:
        return f" ({diff})"
    return " (no change)"


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.1f}m"
    hours = minutes / 60
    return f"{hours:.1f}h"


def render_cli(summary: AnalyticsSummary) -> str:
    """Render the analytics dashboard as a CLI-friendly string."""
    lines: list[str] = []
    lines.append("=" * 50)
    lines.append("  TODO Automation Dashboard")
    lines.append("=" * 50)
    lines.append("")

    # --- Last runs ---
    if summary.scanner_runs:
        last_scan = summary.scanner_runs[-1]
        lines.append(
            f"Last scan:    {last_scan.started_at[:10]}  "
            f"({last_scan.outcome}, {_format_duration(last_scan.duration_seconds)})"
        )
    else:
        lines.append("Last scan:    (none)")

    if summary.resolver_runs:
        last_res = summary.resolver_runs[-1]
        lines.append(
            f"Last resolve: {last_res.started_at[:10]}  "
            f"({last_res.outcome}, {_format_duration(last_res.duration_seconds)}, "
            f"{last_res.budget_utilization_pct}% budget used)"
        )
    else:
        lines.append("Last resolve: (none)")

    lines.append("")

    # --- Backlog ---
    lines.append("--- Backlog ---")
    if summary.latest_debt:
        d = summary.latest_debt
        raw_trend = _trend_arrow(summary.debt_trend)
        lines.append(f"Total TODOs:    {d.total_raw} raw -> {d.total_analyzed} analyzed{raw_trend}")
        for imp in IMPORTANCE_ORDER:
            count = d.by_importance.get(imp, 0)
            imp_trend = _trend_arrow(
                [(date, profile.by_importance.get(imp, 0))
                 for date, profile in
                 [(r.scan_date[:10], compute_debt_profile(r)) for r in summary.reports]]
            ) if len(summary.reports) >= 2 else ""
            lines.append(f"  {imp.capitalize():12s} {count:4d}{imp_trend}")
        lines.append(f"  Actionable:   {d.actionable_pct}% ({d.actionable_count}/{d.total_analyzed})")
    else:
        lines.append("  No scan data available")

    lines.append("")

    # --- Debt Composition ---
    if summary.latest_debt:
        d = summary.latest_debt
        lines.append("--- Debt Composition ---")
        lines.append("By complexity:")
        for cplx in ["trivial", "moderate", "complex", "architectural"]:
            count = d.by_complexity.get(cplx, 0)
            if count:
                lines.append(f"  {cplx:15s} {count:4d}")
        lines.append("By category:")
        for cat, count in sorted(d.by_category.items(), key=lambda x: -x[1]):
            lines.append(f"  {cat:15s} {count:4d}")
        lines.append("")

    # --- Cross-scan diff ---
    if summary.diff_from_previous:
        diff = summary.diff_from_previous
        lines.append("--- Changes Since Last Scan ---")
        lines.append(f"New TODOs:     {len(diff.new_items)}")
        lines.append(f"Removed TODOs: {len(diff.removed_items)}")
        lines.append(f"Net change:    {diff.net_change:+d}")
        lines.append("")

    # --- Resolution Progress ---
    lines.append("--- Resolution Progress ---")
    if summary.resolver_runs:
        lines.append(
            f"Cumulative:    {summary.cumulative_fixed} fixed, "
            f"{summary.cumulative_issues} issues created, "
            f"{summary.cumulative_skipped} skipped"
        )
        lines.append(f"PRs created:   {summary.cumulative_prs}")

        last_res = summary.resolver_runs[-1]
        lines.append(
            f"Last run:      {last_res.items_fixed} fixed, "
            f"{last_res.items_issue_created} issues, "
            f"{last_res.items_skipped} skipped, "
            f"{last_res.items_not_reached} not reached"
        )

        last_fix_rate = (
            round((last_res.items_fixed / last_res.items_attempted) * 100, 1)
            if last_res.items_attempted > 0
            else 0
        )
        lines.append(
            f"Fix rate:      {last_fix_rate}% (last run) | "
            f"{summary.all_time_fix_rate}% (all-time)"
        )

        if summary.backlog_trend:
            first_pending = summary.backlog_trend[0][1]
            last_pending = summary.backlog_trend[-1][1]
            delta = last_pending - first_pending
            lines.append(
                f"Backlog trend: {first_pending} -> {last_pending} pending "
                f"({delta:+d} over {len(summary.reports)} report(s))"
            )
    else:
        lines.append("  No resolver runs yet")

    lines.append("")

    # --- System Health ---
    lines.append("--- System Health ---")
    scan_total = len(summary.scanner_runs)
    res_total = len(summary.resolver_runs)

    if scan_total:
        scan_ok = sum(1 for r in summary.scanner_runs if r.outcome == OUTCOME_SUCCESS)
        lines.append(f"Scanner:  {scan_ok}/{scan_total} runs successful ({summary.scanner_success_rate}%)")
    else:
        lines.append("Scanner:  no runs")

    if res_total:
        res_ok = sum(1 for r in summary.resolver_runs if r.outcome == OUTCOME_SUCCESS)
        lines.append(f"Resolver: {res_ok}/{res_total} runs successful ({summary.resolver_success_rate}%)")
    else:
        lines.append("Resolver: no runs")

    # Average durations
    if summary.scanner_runs:
        avg_scan = sum(r.duration_seconds for r in summary.scanner_runs) / scan_total
        lines.append(f"Avg scan duration:    {_format_duration(avg_scan)}")
    if summary.resolver_runs:
        avg_res = sum(r.duration_seconds for r in summary.resolver_runs) / res_total
        avg_util = sum(r.budget_utilization_pct for r in summary.resolver_runs) / res_total
        lines.append(f"Avg resolve duration: {_format_duration(avg_res)} ({avg_util:.0f}% budget)")

    # Throughput
    if summary.resolver_runs:
        total_attempted = sum(r.items_attempted for r in summary.resolver_runs)
        total_duration_min = sum(r.duration_seconds for r in summary.resolver_runs) / 60
        if total_duration_min > 0:
            throughput = total_attempted / total_duration_min
            lines.append(f"Avg throughput:       {throughput:.2f} items/min")

    lines.append("")

    # --- Run History ---
    lines.append("--- Run History ---")
    all_runs = sorted(
        summary.scanner_runs + summary.resolver_runs,
        key=lambda r: r.started_at,
    )
    for run in all_runs:
        extra = ""
        if run.automation == "resolver":
            extra = f" | fixed={run.items_fixed} issues={run.items_issue_created}"
        elif run.automation == "scanner":
            extra = f" | analyzed={run.todos_analyzed}"
        lines.append(
            f"  {run.started_at[:10]} {run.automation:8s} {run.outcome:7s} "
            f"{_format_duration(run.duration_seconds):>6s}{extra}"
        )

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Markdown output
# ---------------------------------------------------------------------------


def render_markdown(summary: AnalyticsSummary) -> str:
    """Render the analytics as a Markdown summary file."""
    lines: list[str] = []
    lines.append("# TODO Automation Analytics")
    lines.append("")

    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    lines.append(f"*Generated: {now}*")
    lines.append("")

    # Overview table
    lines.append("## Overview")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")

    if summary.scanner_runs:
        last_scan = summary.scanner_runs[-1]
        lines.append(f"| Last scan | {last_scan.started_at[:10]} ({last_scan.outcome}) |")
    if summary.resolver_runs:
        last_res = summary.resolver_runs[-1]
        lines.append(f"| Last resolve | {last_res.started_at[:10]} ({last_res.outcome}) |")
    if summary.latest_debt:
        d = summary.latest_debt
        lines.append(f"| Total raw TODOs | {d.total_raw} |")
        lines.append(f"| Analyzed TODOs | {d.total_analyzed} |")
        lines.append(f"| Actionable | {d.actionable_pct}% ({d.actionable_count}/{d.total_analyzed}) |")
    lines.append(f"| Cumulative fixed | {summary.cumulative_fixed} |")
    lines.append(f"| Cumulative issues created | {summary.cumulative_issues} |")
    lines.append(f"| All-time fix rate | {summary.all_time_fix_rate}% |")
    lines.append(f"| Scanner success rate | {summary.scanner_success_rate}% ({len(summary.scanner_runs)} runs) |")
    lines.append(f"| Resolver success rate | {summary.resolver_success_rate}% ({len(summary.resolver_runs)} runs) |")
    lines.append("")

    # Debt profile
    if summary.latest_debt:
        d = summary.latest_debt
        lines.append("## Current Debt Profile")
        lines.append("")
        lines.append("### By Importance")
        lines.append("")
        lines.append("| Level | Count |")
        lines.append("|-------|-------|")
        for imp in IMPORTANCE_ORDER:
            count = d.by_importance.get(imp, 0)
            lines.append(f"| {imp.capitalize()} | {count} |")
        lines.append("")

        lines.append("### By Complexity")
        lines.append("")
        lines.append("| Level | Count |")
        lines.append("|-------|-------|")
        for cplx in ["trivial", "moderate", "complex", "architectural"]:
            count = d.by_complexity.get(cplx, 0)
            if count:
                lines.append(f"| {cplx.capitalize()} | {count} |")
        lines.append("")

        lines.append("### By Category")
        lines.append("")
        lines.append("| Category | Count |")
        lines.append("|----------|-------|")
        for cat, count in sorted(d.by_category.items(), key=lambda x: -x[1]):
            lines.append(f"| {cat} | {count} |")
        lines.append("")

    # Cross-scan diff
    if summary.diff_from_previous:
        diff = summary.diff_from_previous
        lines.append("## Changes Since Last Scan")
        lines.append("")
        lines.append(f"- **New TODOs**: {len(diff.new_items)}")
        lines.append(f"- **Removed TODOs**: {len(diff.removed_items)}")
        lines.append(f"- **Net change**: {diff.net_change:+d}")
        lines.append("")
        if diff.new_items:
            lines.append("<details>")
            lines.append("<summary>New items</summary>")
            lines.append("")
            for item in diff.new_items:
                lines.append(f"- `{item}`")
            lines.append("")
            lines.append("</details>")
            lines.append("")
        if diff.removed_items:
            lines.append("<details>")
            lines.append("<summary>Removed items</summary>")
            lines.append("")
            for item in diff.removed_items:
                lines.append(f"- `{item}`")
            lines.append("")
            lines.append("</details>")
            lines.append("")

    # Trends
    if len(summary.debt_trend) > 1:
        lines.append("## Trends")
        lines.append("")
        lines.append("### Raw TODO Count Over Time")
        lines.append("")
        lines.append("| Date | Raw TODOs | Critical+High |")
        lines.append("|------|-----------|---------------|")
        for i, (date, raw) in enumerate(summary.debt_trend):
            ch = summary.critical_high_trend[i][1] if i < len(summary.critical_high_trend) else ""
            lines.append(f"| {date} | {raw} | {ch} |")
        lines.append("")

    if len(summary.backlog_trend) > 1:
        lines.append("### Pending Backlog Over Time")
        lines.append("")
        lines.append("| Date | Pending |")
        lines.append("|------|---------|")
        for date, pending in summary.backlog_trend:
            lines.append(f"| {date} | {pending} |")
        lines.append("")

    # Resolution progress
    if summary.resolver_runs:
        lines.append("## Resolution Progress")
        lines.append("")
        lines.append("| Metric | Last Run | All-Time |")
        lines.append("|--------|----------|----------|")
        last = summary.resolver_runs[-1]
        last_fix_rate = (
            round((last.items_fixed / last.items_attempted) * 100, 1)
            if last.items_attempted > 0
            else 0
        )
        lines.append(f"| Items attempted | {last.items_attempted} | {sum(r.items_attempted for r in summary.resolver_runs)} |")
        lines.append(f"| Fixed | {last.items_fixed} | {summary.cumulative_fixed} |")
        lines.append(f"| Issues created | {last.items_issue_created} | {summary.cumulative_issues} |")
        lines.append(f"| Skipped | {last.items_skipped} | {summary.cumulative_skipped} |")
        lines.append(f"| Fix rate | {last_fix_rate}% | {summary.all_time_fix_rate}% |")
        lines.append(f"| PRs created | {last.prs_created} | {summary.cumulative_prs} |")
        lines.append("")

    # Run history
    all_runs = sorted(
        summary.scanner_runs + summary.resolver_runs,
        key=lambda r: r.started_at,
    )
    if all_runs:
        lines.append("## Run History")
        lines.append("")
        lines.append("| Date | Type | Outcome | Duration | Details |")
        lines.append("|------|------|---------|----------|---------|")
        for run in all_runs:
            details = ""
            if run.automation == "scanner":
                details = f"{run.todos_analyzed} analyzed"
            elif run.automation == "resolver":
                details = (
                    f"{run.items_fixed}F/{run.items_issue_created}I/"
                    f"{run.items_skipped}S | {run.budget_utilization_pct}% budget"
                )
            lines.append(
                f"| {run.started_at[:10]} | {run.automation} | {run.outcome} | "
                f"{_format_duration(run.duration_seconds)} | {details} |"
            )
        lines.append("")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="TODO automation analytics dashboard")
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config file (default: config.yaml)",
    )
    parser.add_argument(
        "--output-md",
        action="store_true",
        help="Generate reports/analytics_summary.md",
    )
    args = parser.parse_args()

    # Load config
    config_path = Path(args.config)
    if not config_path.exists():
        logger.error("Config file not found: %s", config_path)
        sys.exit(1)
    with open(config_path) as f:
        config = yaml.safe_load(f)

    report_dir = config["reports"]["output_dir"]

    # Load all reports
    reports = load_all_reports(report_dir)
    if not reports:
        print("No reports found. Run the scanner first: python -m src.scanner")
        sys.exit(0)

    logger.info("Loaded %d report(s) from %s", len(reports), report_dir)

    # Compute analytics
    summary = compute_analytics(reports)

    # CLI output
    print(render_cli(summary))

    # Markdown output
    if args.output_md:
        md_path = Path(report_dir) / "analytics_summary.md"
        md_path.write_text(render_markdown(summary))
        print(f"Markdown summary saved to: {md_path}")


if __name__ == "__main__":
    main()
