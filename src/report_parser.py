"""Parse and update TODO scan reports.

Reports are stored as JSON (machine-readable) alongside a generated Markdown
summary (human-readable). The resolver reads the JSON; the Markdown is for
humans reviewing the report in the repo.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Statuses a TODO item can have
STATUS_PENDING = "PENDING"
STATUS_DONE = "DONE"
STATUS_ISSUE_CREATED = "ISSUE_CREATED"
STATUS_SKIPPED = "SKIPPED"
STATUS_IN_PROGRESS = "IN_PROGRESS"


@dataclass
class TodoItem:
    file: str
    line: int
    marker: str  # TODO, FIXME, HACK, XXX
    text: str  # the marker comment text
    importance: str  # critical, high, medium, low
    complexity: str  # trivial, moderate, complex, architectural
    category: str  # bug_fix, error_handling, performance, refactor, cleanup, etc.
    reasoning: str  # why this ranking
    actionable: bool
    status: str = STATUS_PENDING
    resolution_note: str = ""
    pr_url: str = ""
    issue_url: str = ""


@dataclass
class TodoReport:
    scan_date: str
    repo: str
    branch: str
    summary: str
    todos: list[TodoItem] = field(default_factory=list)


def save_report(report: TodoReport, output_dir: str | Path) -> tuple[Path, Path]:
    """Save the report as JSON + Markdown. Returns (json_path, md_path)."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    date_slug = report.scan_date[:10]  # YYYY-MM-DD
    json_path = output_dir / f"todo_scan_{date_slug}.json"
    md_path = output_dir / f"todo_scan_{date_slug}.md"

    # JSON
    json_path.write_text(json.dumps(asdict(report), indent=2) + "\n")
    logger.info("Saved JSON report to %s", json_path)

    # Markdown
    md_path.write_text(_render_markdown(report))
    logger.info("Saved Markdown report to %s", md_path)

    return json_path, md_path


def load_latest_report(output_dir: str | Path) -> TodoReport | None:
    """Load the most recent JSON report from the output directory."""
    output_dir = Path(output_dir)
    json_files = sorted(output_dir.glob("todo_scan_*.json"), reverse=True)
    if not json_files:
        logger.warning("No reports found in %s", output_dir)
        return None
    return load_report(json_files[0])


def load_report(path: Path) -> TodoReport:
    """Load a report from a JSON file."""
    data = json.loads(path.read_text())
    todos = [TodoItem(**t) for t in data.pop("todos", [])]
    return TodoReport(**data, todos=todos)


def update_report(report: TodoReport, output_dir: str | Path) -> tuple[Path, Path]:
    """Re-save a report after status updates."""
    return save_report(report, output_dir)


def _render_markdown(report: TodoReport) -> str:
    lines = [
        f"# TODO Scan Report \u2014 {report.scan_date[:10]}",
        "",
        f"**Repo**: `{report.repo}` (branch: `{report.branch}`)",
        f"**Scanned**: {report.scan_date}",
        "",
        "## Summary",
        "",
        report.summary,
        "",
        "## Items by Priority",
        "",
    ]

    # Group by importance
    for importance in ["critical", "high", "medium", "low"]:
        items = [t for t in report.todos if t.importance == importance]
        if not items:
            continue
        lines.append(f"### {importance.upper()} ({len(items)})")
        lines.append("")
        lines.append("| # | Status | File | Marker | Complexity | Category | Text |")
        lines.append("|---|--------|------|--------|------------|----------|------|")
        for i, t in enumerate(items, 1):
            status_icon = {
                STATUS_PENDING: "\u23f3",
                STATUS_DONE: "\u2714\ufe0f",
                STATUS_ISSUE_CREATED: "\U0001f4cb",
                STATUS_SKIPPED: "\u23ed\ufe0f",
                STATUS_IN_PROGRESS: "\U0001f527",
            }.get(t.status, t.status)
            text_preview = t.text[:60] + ("..." if len(t.text) > 60 else "")
            lines.append(
                f"| {i} | {status_icon} {t.status} | `{t.file}:{t.line}` | "
                f"{t.marker} | {t.complexity} | {t.category} | {text_preview} |"
            )
        lines.append("")

    # Detailed reasoning section
    lines.append("## Detailed Reasoning")
    lines.append("")
    for i, t in enumerate(report.todos, 1):
        lines.append(f"### {i}. `{t.file}:{t.line}` ({t.marker})")
        lines.append("")
        lines.append(f"**Text**: {t.text}")
        lines.append(f"**Importance**: {t.importance} | **Complexity**: {t.complexity} | "
                      f"**Category**: {t.category}")
        lines.append(f"**Actionable**: {'Yes' if t.actionable else 'No'}")
        lines.append(f"**Reasoning**: {t.reasoning}")
        if t.status != STATUS_PENDING:
            lines.append(f"**Status**: {t.status}")
        if t.resolution_note:
            lines.append(f"**Resolution**: {t.resolution_note}")
        if t.pr_url:
            lines.append(f"**PR**: {t.pr_url}")
        if t.issue_url:
            lines.append(f"**Issue**: {t.issue_url}")
        lines.append("")

    return "\n".join(lines) + "\n"


def get_pending_todos(report: TodoReport) -> list[TodoItem]:
    """Return pending + actionable items, sorted by importance."""
    importance_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    pending = [t for t in report.todos if t.status == STATUS_PENDING and t.actionable]
    return sorted(pending, key=lambda t: importance_order.get(t.importance, 99))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
