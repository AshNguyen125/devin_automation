"""Automation 1: Scan superset_fork for TODO/FIXME/HACK markers and rank them.

Usage:
    python -m src.scanner [--config config.yaml] [--repo-path ../superset_fork]
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from pathlib import Path

import yaml

from src.devin_client import DevinClient
from src.report_parser import TodoItem, TodoReport, now_iso, save_report

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

PROMPT_TEMPLATE_PATH = Path(__file__).parent / "prompts" / "scanner_prompt.md"

# JSON Schema for the scanner's structured output
SCANNER_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "todos": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "marker": {"type": "string"},
                    "text": {"type": "string"},
                    "importance": {
                        "type": "string",
                        "enum": ["critical", "high", "medium", "low"],
                    },
                    "complexity": {
                        "type": "string",
                        "enum": ["trivial", "moderate", "complex", "architectural"],
                    },
                    "category": {
                        "type": "string",
                        "enum": [
                            "bug_fix",
                            "error_handling",
                            "performance",
                            "refactor",
                            "cleanup",
                            "feature",
                            "documentation",
                            "testing",
                            "security",
                        ],
                    },
                    "reasoning": {"type": "string"},
                    "actionable": {"type": "boolean"},
                },
                "required": [
                    "file",
                    "line",
                    "marker",
                    "text",
                    "importance",
                    "complexity",
                    "category",
                    "reasoning",
                    "actionable",
                ],
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["todos", "summary"],
}


def collect_todos(repo_path: Path, config: dict) -> list[dict]:
    """Run grep on the local checkout to find TODO markers with context."""
    scanner_cfg = config["scanner"]
    markers = scanner_cfg["markers"]
    scan_dirs = scanner_cfg["scan_dirs"]
    context_lines = scanner_cfg.get("context_lines", 5)
    max_todos = scanner_cfg.get("max_todos", 100)

    pattern = "|".join(markers)
    todos: list[dict] = []

    for scan_dir in scan_dirs:
        full_dir = repo_path / scan_dir
        if not full_dir.exists():
            logger.warning("Scan directory not found: %s", full_dir)
            continue

        # Determine file extensions based on directory
        if "frontend" in scan_dir:
            include_args = ["--include=*.ts", "--include=*.tsx", "--include=*.js", "--include=*.jsx"]
        else:
            include_args = ["--include=*.py"]

        cmd = [
            "grep",
            "-rn",
            f"-C{context_lines}",
            "-E",
            pattern,
            *include_args,
            str(full_dir),
        ]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            logger.warning("grep timed out for %s", scan_dir)
            continue

        if result.returncode not in (0, 1):  # 1 = no matches
            logger.warning("grep error for %s: %s", scan_dir, result.stderr)
            continue

        todos.extend(_parse_grep_output(result.stdout, repo_path, markers))

    # Deduplicate by file:line
    seen: set[str] = set()
    unique: list[dict] = []
    for t in todos:
        key = f"{t['file']}:{t['line']}"
        if key not in seen:
            seen.add(key)
            unique.append(t)

    # Cap at max_todos (take a representative sample if over limit)
    if len(unique) > max_todos:
        logger.info("Found %d TODOs, capping at %d", len(unique), max_todos)
        unique = unique[:max_todos]

    logger.info("Collected %d TODO markers", len(unique))
    return unique


def _parse_grep_output(output: str, repo_path: Path, markers: list[str]) -> list[dict]:
    """Parse grep -n output into structured TODO entries."""
    todos: list[dict] = []
    marker_pattern = re.compile(r"\b(" + "|".join(markers) + r")\b[:\s]*(.*)", re.IGNORECASE)

    for line in output.splitlines():
        # Match lines like: path/to/file.py:42: # TODO: fix this
        match = re.match(r"^(.+?):(\d+)[:|-](.*)", line)
        if not match:
            continue
        filepath, lineno, content = match.groups()

        # Only include lines that contain a marker (skip context lines)
        marker_match = marker_pattern.search(content)
        if not marker_match:
            continue

        # Make path relative to repo root
        rel_path = filepath
        try:
            rel_path = str(Path(filepath).relative_to(repo_path))
        except ValueError:
            pass

        todos.append(
            {
                "file": rel_path,
                "line": int(lineno),
                "marker": marker_match.group(1).upper(),
                "text": marker_match.group(2).strip(),
            }
        )

    return todos


def build_prompt(todos: list[dict], config: dict) -> str:
    """Build the scanner prompt with the collected TODO list."""
    template = PROMPT_TEMPLATE_PATH.read_text()

    # Format the TODO list
    todo_lines = []
    for i, t in enumerate(todos, 1):
        todo_lines.append(f"{i}. `{t['file']}:{t['line']}` [{t['marker']}] {t['text']}")

    todo_list = "\n".join(todo_lines)

    repo_cfg = config["target_repo"]
    return template.format(
        repo_owner=repo_cfg["owner"],
        repo_name=repo_cfg["name"],
        branch=repo_cfg["branch"],
        todo_list=todo_list,
    )


def run_scanner(config: dict, repo_path: Path, api_key: str) -> TodoReport:
    """Run the full scanner pipeline."""
    # 1. Collect TODOs from local checkout
    todos = collect_todos(repo_path, config)
    if not todos:
        logger.warning("No TODOs found in repo — nothing to scan")
        return TodoReport(
            scan_date=now_iso(),
            repo=f"{config['target_repo']['owner']}/{config['target_repo']['name']}",
            branch=config["target_repo"]["branch"],
            summary="No TODO markers found.",
            todos=[],
        )

    # 2. Build prompt
    prompt = build_prompt(todos, config)
    logger.info("Built prompt with %d TODOs (%d chars)", len(todos), len(prompt))

    # 3. Create Devin session
    client = DevinClient(api_key=api_key, org_id=config["devin"]["org_id"])
    repo_url = f"github.com/{config['target_repo']['owner']}/{config['target_repo']['name']}"

    session = client.create_session(
        prompt=prompt,
        repos=[repo_url],
        title=f"TODO Scanner — {config['target_repo']['name']}",
        tags=["automation", "todo-scanner"],
        structured_output_schema=SCANNER_OUTPUT_SCHEMA,
        structured_output_required=True,
    )
    logger.info("Created scanner session: %s", session.url)
    print(f"\nScanner session created: {session.url}")
    print("Waiting for Devin to analyze TODOs...\n")

    # 4. Poll until complete
    result = client.poll_until_done(session.session_id, timeout_seconds=1800)

    if result.status == "error":
        logger.error("Scanner session failed: %s", result.url)
        sys.exit(1)

    # 5. Parse structured output into report
    report = _build_report_from_output(result.structured_output, config, todos)

    # 6. Save report
    repo_cfg = config["target_repo"]
    report_dir = config["reports"]["output_dir"]
    json_path, md_path = save_report(report, report_dir)
    print(f"\nReport saved:")
    print(f"  JSON: {json_path}")
    print(f"  Markdown: {md_path}")

    # Summary
    importance_counts = {}
    for t in report.todos:
        importance_counts[t.importance] = importance_counts.get(t.importance, 0) + 1
    print(f"\nResults: {len(report.todos)} TODOs analyzed")
    for imp in ["critical", "high", "medium", "low"]:
        count = importance_counts.get(imp, 0)
        if count:
            print(f"  {imp}: {count}")

    return report


def _build_report_from_output(
    structured_output: dict | None,
    config: dict,
    raw_todos: list[dict],
) -> TodoReport:
    """Convert Devin's structured output to a TodoReport."""
    repo_cfg = config["target_repo"]

    if not structured_output:
        logger.warning("No structured output from scanner session; using raw TODO list")
        return TodoReport(
            scan_date=now_iso(),
            repo=f"{repo_cfg['owner']}/{repo_cfg['name']}",
            branch=repo_cfg["branch"],
            summary="Scanner session did not produce structured output.",
            todos=[
                TodoItem(
                    file=t["file"],
                    line=t["line"],
                    marker=t["marker"],
                    text=t["text"],
                    importance="medium",
                    complexity="moderate",
                    category="cleanup",
                    reasoning="Not analyzed — scanner session did not produce output",
                    actionable=True,
                )
                for t in raw_todos
            ],
        )

    return TodoReport(
        scan_date=now_iso(),
        repo=f"{repo_cfg['owner']}/{repo_cfg['name']}",
        branch=repo_cfg["branch"],
        summary=structured_output.get("summary", ""),
        todos=[
            TodoItem(
                file=t["file"],
                line=t["line"],
                marker=t["marker"],
                text=t["text"],
                importance=t["importance"],
                complexity=t["complexity"],
                category=t["category"],
                reasoning=t["reasoning"],
                actionable=t["actionable"],
            )
            for t in structured_output.get("todos", [])
        ],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan superset_fork for TODOs and rank them")
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config file (default: config.yaml)",
    )
    parser.add_argument(
        "--repo-path",
        default=None,
        help="Path to local superset_fork checkout (default: ../superset_fork)",
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

    # Resolve repo path
    if args.repo_path:
        repo_path = Path(args.repo_path)
    else:
        # Default: sibling directory
        repo_path = Path.cwd().parent / config["target_repo"]["name"]
    if not repo_path.exists():
        logger.error(
            "Repo not found at %s. Clone it or pass --repo-path.",
            repo_path,
        )
        sys.exit(1)
    logger.info("Using repo at: %s", repo_path)

    # Resolve API key
    import os

    api_key = args.api_key or os.environ.get("DEVIN_API_KEY", "")
    if not api_key:
        logger.error("No API key. Set DEVIN_API_KEY or pass --api-key.")
        sys.exit(1)

    run_scanner(config, repo_path, api_key)


if __name__ == "__main__":
    main()
