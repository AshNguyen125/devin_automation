"""Automation 2: Resolve TODO items from the latest scan report.

Creates a Devin session that works through the ranked TODO list, fixing items
or creating GitHub issues, within a configurable time budget.

Usage:
    python -m src.resolver [--config config.yaml] [--report reports/todo_scan_YYYY-MM-DD.json]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

import yaml

from src.devin_client import DevinClient
from src.report_parser import (
    OUTCOME_FAILURE,
    OUTCOME_PARTIAL,
    OUTCOME_SUCCESS,
    STATUS_DONE,
    STATUS_ISSUE_CREATED,
    STATUS_SKIPPED,
    RunMetadata,
    TodoReport,
    get_pending_todos,
    load_latest_report,
    load_report,
    now_iso,
    update_report,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

PROMPT_TEMPLATE_PATH = Path(__file__).parent / "prompts" / "resolver_prompt.md"

# JSON Schema for the resolver's structured output
RESOLVER_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "resolved_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "marker": {"type": "string"},
                    "action": {
                        "type": "string",
                        "enum": ["fixed", "issue_created", "skipped", "not_reached"],
                    },
                    "pr_url": {"type": "string"},
                    "issue_url": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["file", "line", "marker", "action", "note"],
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["resolved_items", "summary"],
}


def build_resolver_prompt(pending_todos: list, config: dict) -> str:
    """Build the resolver prompt with the pending TODO items."""
    template = PROMPT_TEMPLATE_PATH.read_text()

    # Format the TODO items with full context
    item_lines = []
    for i, t in enumerate(pending_todos, 1):
        item_lines.append(f"### Item {i}: `{t.file}:{t.line}` [{t.marker}]")
        item_lines.append(f"**Text**: {t.text}")
        item_lines.append(f"**Importance**: {t.importance} | **Complexity**: {t.complexity}")
        item_lines.append(f"**Category**: {t.category}")
        item_lines.append(f"**Context**: {t.reasoning}")
        item_lines.append("")

    todo_items = "\n".join(item_lines)
    repo_cfg = config["target_repo"]
    resolver_cfg = config["resolver"]

    return template.format(
        repo_owner=repo_cfg["owner"],
        repo_name=repo_cfg["name"],
        branch=repo_cfg["branch"],
        todo_items=todo_items,
        budget_minutes=resolver_cfg["time_budget_minutes"],
    )


def run_resolver(config: dict, report: TodoReport, api_key: str) -> None:
    """Run the resolver pipeline."""
    started_at = now_iso()
    start_time = time.monotonic()
    resolver_cfg = config["resolver"]
    budget_minutes = resolver_cfg["time_budget_minutes"]

    # 1. Get pending items
    pending = get_pending_todos(report)
    max_items = resolver_cfg.get("max_items", 10)
    if len(pending) > max_items:
        logger.info("Capping at %d items (out of %d pending)", max_items, len(pending))
        pending = pending[:max_items]

    if not pending:
        print("No pending actionable TODOs to resolve. Run the scanner first.")
        return

    print(f"Found {len(pending)} pending TODOs to resolve:")
    for i, t in enumerate(pending, 1):
        print(f"  {i}. [{t.importance}] {t.file}:{t.line} — {t.text[:60]}")
    print()

    # 2. Build prompt
    prompt = build_resolver_prompt(pending, config)
    logger.info("Built resolver prompt (%d chars) for %d items", len(prompt), len(pending))

    # 3. Create Devin session
    client = DevinClient(api_key=api_key, org_id=config["devin"]["org_id"])
    repo_url = f"github.com/{config['target_repo']['owner']}/{config['target_repo']['name']}"

    session = client.create_session(
        prompt=prompt,
        repos=[repo_url],
        title=f"TODO Resolver — {config['target_repo']['name']}",
        tags=["automation", "todo-resolver"],
        structured_output_schema=RESOLVER_OUTPUT_SCHEMA,
        structured_output_required=True,
    )
    logger.info("Created resolver session: %s", session.url)
    print(f"Resolver session created: {session.url}")
    print(
        f"Time budget: {budget_minutes} minutes "
        f"(wrap-up at {budget_minutes - resolver_cfg['wrap_up_buffer_minutes']} min)\n"
    )
    print("Waiting for Devin to resolve TODOs...\n")

    # 4. Poll with time budget
    result = client.poll_with_budget(
        session.session_id,
        budget_minutes=budget_minutes,
        wrap_up_buffer_minutes=resolver_cfg["wrap_up_buffer_minutes"],
    )

    finished_at = now_iso()
    duration = time.monotonic() - start_time
    budget_used = min(duration / 60, budget_minutes)

    # 5. Update report with results
    _apply_results_to_report(report, result.structured_output)

    # Count resolution outcomes
    resolved_items = (result.structured_output or {}).get("resolved_items", [])
    items_fixed = sum(1 for r in resolved_items if r["action"] == "fixed")
    items_issue = sum(1 for r in resolved_items if r["action"] == "issue_created")
    items_skipped = sum(1 for r in resolved_items if r["action"] == "skipped")
    items_not_reached = sum(1 for r in resolved_items if r["action"] == "not_reached")
    prs_created = len(result.pull_requests)
    issues_created = items_issue

    # Determine outcome
    if result.status == "error":
        outcome = OUTCOME_FAILURE
    elif resolved_items:
        outcome = OUTCOME_SUCCESS if items_fixed > 0 or items_issue > 0 else OUTCOME_PARTIAL
    elif result.structured_output:
        outcome = OUTCOME_PARTIAL
    else:
        outcome = OUTCOME_FAILURE

    # Build run metadata
    meta = RunMetadata(
        automation="resolver",
        run_id=f"resolve-{started_at}",
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=round(duration, 1),
        devin_session_id=session.session_id,
        devin_session_url=session.url,
        devin_session_status=result.status,
        devin_session_status_detail=result.status_detail or "",
        outcome=outcome,
        budget_minutes=budget_minutes,
        budget_used_minutes=round(budget_used, 1),
        budget_utilization_pct=round((budget_used / budget_minutes) * 100, 1) if budget_minutes else 0,
        items_attempted=len(pending),
        items_fixed=items_fixed,
        items_issue_created=items_issue,
        items_skipped=items_skipped,
        items_not_reached=items_not_reached,
        prs_created=prs_created,
        issues_created=issues_created,
    )
    report.run_history.append(meta)

    # 6. Save updated report
    report_dir = config["reports"]["output_dir"]
    json_path, md_path = update_report(report, report_dir)
    print(f"\nUpdated report:")
    print(f"  JSON: {json_path}")
    print(f"  Markdown: {md_path}")

    # 7. Print summary
    if result.structured_output:
        summary = result.structured_output.get("summary", "No summary")
        print(f"\nSession summary: {summary}")
        for item in resolved_items:
            action_icon = {
                "fixed": "PR",
                "issue_created": "ISSUE",
                "skipped": "SKIP",
                "not_reached": "---",
            }.get(item["action"], item["action"])
            print(f"  [{action_icon}] {item['file']}:{item['line']} — {item['note']}")
            if item.get("pr_url"):
                print(f"         PR: {item['pr_url']}")
            if item.get("issue_url"):
                print(f"         Issue: {item['issue_url']}")

    if result.pull_requests:
        print(f"\nPRs created:")
        for pr in result.pull_requests:
            print(f"  {pr['pr_url']} ({pr.get('pr_state', 'open')})")

    print(f"\nDuration: {duration:.0f}s | Budget used: {budget_used:.1f}/{budget_minutes}min ({meta.budget_utilization_pct}%)")
    print(f"Outcome: {outcome} | Fixed: {items_fixed} | Issues: {items_issue} | Skipped: {items_skipped}")
    print(f"Session: {result.url}")


def _apply_results_to_report(report: TodoReport, structured_output: dict | None) -> None:
    """Update TODO statuses in the report based on resolver results."""
    if not structured_output:
        logger.warning("No structured output from resolver session")
        return

    resolved_items = structured_output.get("resolved_items", [])

    # Build a lookup map
    result_map: dict[str, dict] = {}
    for item in resolved_items:
        key = f"{item['file']}:{item['line']}"
        result_map[key] = item

    for todo in report.todos:
        key = f"{todo.file}:{todo.line}"
        if key not in result_map:
            continue

        result = result_map[key]
        action = result["action"]

        if action == "fixed":
            todo.status = STATUS_DONE
            todo.pr_url = result.get("pr_url", "")
            todo.resolution_note = result.get("note", "Fixed by resolver")
        elif action == "issue_created":
            todo.status = STATUS_ISSUE_CREATED
            todo.issue_url = result.get("issue_url", "")
            todo.resolution_note = result.get("note", "Issue created")
        elif action == "skipped":
            todo.status = STATUS_SKIPPED
            todo.resolution_note = result.get("note", "Skipped")


def main() -> None:
    parser = argparse.ArgumentParser(description="Resolve TODOs from the latest scan report")
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config file (default: config.yaml)",
    )
    parser.add_argument(
        "--report",
        default=None,
        help="Path to a specific report JSON (default: latest in reports/)",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="Devin API key (default: $DEVIN_API_KEY env var)",
    )
    args = parser.parse_args()

    # Load config
    config_path = Path(args.config)
    if not config_path.exists():
        logger.error("Config file not found: %s", config_path)
        sys.exit(1)
    with open(config_path) as f:
        config = yaml.safe_load(f)

    # Load report
    if args.report:
        report = load_report(Path(args.report))
    else:
        report = load_latest_report(config["reports"]["output_dir"])

    if report is None:
        logger.error("No report found. Run the scanner first: python -m src.scanner")
        sys.exit(1)

    logger.info("Loaded report from %s (%d items)", report.scan_date, len(report.todos))

    # Resolve API key
    api_key = args.api_key or os.environ.get("DEVIN_API_KEY", "")
    if not api_key:
        logger.error("No API key. Set DEVIN_API_KEY or pass --api-key.")
        sys.exit(1)

    run_resolver(config, report, api_key)


if __name__ == "__main__":
    main()
