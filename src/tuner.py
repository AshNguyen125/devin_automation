"""Meta-automation: review scanner/resolver metrics and tune cadence/budget.

The tuner runs infrequently (e.g. monthly). It:
  1. Loads all reports and computes analytics (reusing analytics.compute_analytics).
  2. Computes deterministic candidate signals.
  3. Creates a Devin session that judges the metrics and proposes config changes,
     returning structured decisions {knob, current, proposed, action, confidence, rationale}.
  4. Applies guardrails (ladder snapping, step clamping, cooldown, confidence floor).
  5. Appends a decision-log entry to reports/tuning_log.md (+ machine-readable .json).
  6. Optionally opens a config.yaml PR with the reasoning in the body.

Usage:
    python -m src.tuner [--config config.yaml] [--dry-run] [--no-pr]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from src.analytics import compute_analytics, load_all_reports
from src.devin_client import DevinClient
from src.report_parser import now_iso

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

PROMPT_TEMPLATE_PATH = Path(__file__).parent / "prompts" / "tuner_prompt.md"

CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}

# JSON Schema for the tuner's structured output
TUNER_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "knob": {"type": "string"},
                    "current_value": {"type": "integer"},
                    "proposed_value": {"type": "integer"},
                    "action": {"type": "string", "enum": ["change", "no_change"]},
                    "confidence": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                    },
                    "rationale": {"type": "string"},
                },
                "required": [
                    "knob",
                    "current_value",
                    "proposed_value",
                    "action",
                    "confidence",
                    "rationale",
                ],
            },
        },
        "overall_assessment": {"type": "string"},
    },
    "required": ["decisions", "overall_assessment"],
}


@dataclass
class AppliedChange:
    """A tuning decision that passed guardrails and was applied."""

    knob: str
    old_value: int
    new_value: int
    confidence: str
    rationale: str


@dataclass
class RejectedChange:
    """A tuning decision that was rejected by a guardrail."""

    knob: str
    proposed_value: int
    reason: str
    rationale: str


# ---------------------------------------------------------------------------
# Knob access helpers (dotted paths into the config dict)
# ---------------------------------------------------------------------------


def get_knob(config: dict[str, Any], knob: str) -> Any:
    """Read a dotted knob path, e.g. 'resolver.time_budget_minutes'."""
    node: Any = config
    for part in knob.split("."):
        node = node[part]
    return node


def set_knob(config: dict[str, Any], knob: str, value: Any) -> None:
    """Set a dotted knob path in-place."""
    parts = knob.split(".")
    node = config
    for part in parts[:-1]:
        node = node[part]
    node[parts[-1]] = value


# ---------------------------------------------------------------------------
# Deterministic signals
# ---------------------------------------------------------------------------


def compute_signals(summary: Any) -> dict[str, Any]:
    """Pre-compute candidate signals to focus the Devin session's attention."""
    signals: dict[str, Any] = {}

    scanner_runs = summary.scanner_runs
    resolver_runs = summary.resolver_runs

    signals["scanner_run_count"] = len(scanner_runs)
    signals["resolver_run_count"] = len(resolver_runs)
    signals["scanner_success_rate_pct"] = summary.scanner_success_rate
    signals["resolver_success_rate_pct"] = summary.resolver_success_rate

    # Average new TODOs per scan (from consecutive diffs)
    if len(summary.debt_trend) >= 2:
        deltas = [
            summary.debt_trend[i][1] - summary.debt_trend[i - 1][1]
            for i in range(1, len(summary.debt_trend))
        ]
        signals["avg_raw_todo_delta_per_scan"] = round(sum(deltas) / len(deltas), 1)
    else:
        signals["avg_raw_todo_delta_per_scan"] = None

    # Backlog direction
    if len(summary.backlog_trend) >= 2:
        first = summary.backlog_trend[0][1]
        last = summary.backlog_trend[-1][1]
        signals["backlog_net_change"] = last - first
        signals["backlog_direction"] = (
            "shrinking" if last < first else "growing" if last > first else "flat"
        )
    else:
        signals["backlog_net_change"] = None
        signals["backlog_direction"] = "unknown"

    # Resolver budget utilization + fix rate + duration
    if resolver_runs:
        signals["avg_budget_utilization_pct"] = round(
            sum(r.budget_utilization_pct for r in resolver_runs) / len(resolver_runs), 1
        )
        signals["avg_resolver_duration_min"] = round(
            sum(r.duration_seconds for r in resolver_runs) / len(resolver_runs) / 60, 1
        )
        signals["all_time_fix_rate_pct"] = summary.all_time_fix_rate
        total_attempted = sum(r.items_attempted for r in resolver_runs)
        signals["avg_items_attempted_per_run"] = (
            round(total_attempted / len(resolver_runs), 1) if resolver_runs else 0
        )
        # How often the resolver runs right up against its budget
        at_budget = sum(1 for r in resolver_runs if r.budget_utilization_pct >= 95)
        signals["runs_hitting_budget_ceiling"] = at_budget
    else:
        signals["avg_budget_utilization_pct"] = None
        signals["avg_resolver_duration_min"] = None
        signals["all_time_fix_rate_pct"] = None
        signals["avg_items_attempted_per_run"] = None
        signals["runs_hitting_budget_ceiling"] = None

    return signals


# ---------------------------------------------------------------------------
# Tuning log (cooldown state + human-readable history)
# ---------------------------------------------------------------------------


def load_tuning_history(output_dir: Path) -> list[dict[str, Any]]:
    """Load machine-readable tuning history for cooldown checks."""
    json_path = output_dir / "tuning_log.json"
    if not json_path.exists():
        return []
    try:
        data: list[dict[str, Any]] = json.loads(json_path.read_text())
        return data
    except json.JSONDecodeError:
        logger.warning("Corrupt tuning_log.json; treating as empty")
        return []


def knob_in_cooldown(
    knob: str, history: list[dict[str, Any]], cooldown_days: int, as_of: datetime
) -> bool:
    """Return True if the knob was changed within cooldown_days of `as_of`."""
    cutoff = as_of - timedelta(days=cooldown_days)
    for entry in history:
        for change in entry.get("applied_changes", []):
            if change["knob"] != knob:
                continue
            ts = _parse_iso(entry.get("timestamp", ""))
            if ts and ts >= cutoff:
                return True
    return False


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def append_tuning_log(
    output_dir: Path,
    timestamp: str,
    session_url: str,
    overall_assessment: str,
    applied: list[AppliedChange],
    rejected: list[RejectedChange],
    no_changes: list[dict[str, Any]],
    skipped_reason: str = "",
) -> None:
    """Append a decision entry to tuning_log.md (human) and tuning_log.json (machine)."""
    # --- Machine-readable ---
    json_path = output_dir / "tuning_log.json"
    history = load_tuning_history(output_dir)
    history.append(
        {
            "timestamp": timestamp,
            "session_url": session_url,
            "overall_assessment": overall_assessment,
            "applied_changes": [asdict(c) for c in applied],
            "rejected_changes": [asdict(c) for c in rejected],
            "no_changes": no_changes,
            "skipped_reason": skipped_reason,
        }
    )
    json_path.write_text(json.dumps(history, indent=2) + "\n")

    # --- Human-readable ---
    md_path = output_dir / "tuning_log.md"
    lines: list[str] = []
    if not md_path.exists():
        lines.append("# Tuning Decision Log")
        lines.append("")
        lines.append(
            "Chronological log of cadence/budget tuning decisions made by the "
            "meta-automation (`src/tuner.py`). Newest entries at the bottom."
        )
        lines.append("")
    else:
        lines.append(md_path.read_text().rstrip("\n"))
        lines.append("")

    lines.append(f"## {timestamp[:10]} — Tuning Run")
    lines.append("")
    if session_url:
        lines.append(f"**Devin session**: {session_url}")
        lines.append("")
    if skipped_reason:
        lines.append(f"**Skipped**: {skipped_reason}")
        lines.append("")
    if overall_assessment:
        lines.append(f"**Assessment**: {overall_assessment}")
        lines.append("")

    if applied:
        lines.append("**Applied changes:**")
        lines.append("")
        for c in applied:
            lines.append(
                f"- `{c.knob}`: {c.old_value} → **{c.new_value}** (confidence: {c.confidence})"
            )
            lines.append(f"  - {c.rationale}")
        lines.append("")

    if rejected:
        lines.append("**Rejected changes (guardrails):**")
        lines.append("")
        for r in rejected:
            lines.append(f"- `{r.knob}` → {r.proposed_value}: {r.reason}")
            lines.append(f"  - Proposed because: {r.rationale}")
        lines.append("")

    if no_changes:
        lines.append("**Held steady:**")
        lines.append("")
        for nc in no_changes:
            lines.append(f"- `{nc['knob']}` stays at {nc['current_value']} — {nc['rationale']}")
        lines.append("")

    md_path.write_text("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------


def snap_and_clamp(
    current: int, proposed: int, ladder: list[int], max_steps: int
) -> tuple[int, str]:
    """Snap proposed to nearest ladder rung, clamp to max_steps from current.

    Returns (final_value, note). note is empty if no adjustment was made.
    """
    notes: list[str] = []

    # Snap proposed to nearest ladder value
    snapped = min(ladder, key=lambda v: abs(v - proposed))
    if snapped != proposed:
        notes.append(f"snapped {proposed}→{snapped} onto ladder")

    # Find indices on the ladder
    if current not in ladder:
        # current isn't on the ladder; snap it too for stepping purposes
        cur_snapped = min(ladder, key=lambda v: abs(v - current))
        cur_idx = ladder.index(cur_snapped)
    else:
        cur_idx = ladder.index(current)
    target_idx = ladder.index(snapped)

    # Clamp step size
    delta = target_idx - cur_idx
    if abs(delta) > max_steps:
        clamped_idx = cur_idx + (max_steps if delta > 0 else -max_steps)
        final = ladder[clamped_idx]
        notes.append(f"clamped to {max_steps} step(s): {snapped}→{final}")
    else:
        final = snapped

    return final, "; ".join(notes)


def apply_guardrails(
    decisions: list[dict[str, Any]],
    config: dict[str, Any],
    tuner_cfg: dict[str, Any],
    history: list[dict[str, Any]],
    as_of: datetime,
) -> tuple[list[AppliedChange], list[RejectedChange], list[dict[str, Any]]]:
    """Filter Devin's decisions through the guardrails.

    Returns (applied, rejected, no_changes).
    """
    ladders: dict[str, list[int]] = tuner_cfg["ladders"]
    max_steps = tuner_cfg.get("max_steps_per_change", 1)
    cooldown_days = tuner_cfg.get("cooldown_days", 30)
    min_conf = CONFIDENCE_RANK.get(tuner_cfg.get("min_confidence", "medium"), 1)

    applied: list[AppliedChange] = []
    rejected: list[RejectedChange] = []
    no_changes: list[dict[str, Any]] = []

    for d in decisions:
        knob = d["knob"]
        proposed = d["proposed_value"]
        action = d["action"]
        confidence = d.get("confidence", "low")
        rationale = d.get("rationale", "")

        # Always read the *actual* current value from config (don't trust the model)
        if knob not in ladders:
            rejected.append(RejectedChange(knob, proposed, "unknown knob (not tunable)", rationale))
            continue

        try:
            actual_current = int(get_knob(config, knob))
        except (KeyError, TypeError, ValueError):
            rejected.append(RejectedChange(knob, proposed, "knob missing from config", rationale))
            continue

        if action == "no_change":
            no_changes.append(
                {"knob": knob, "current_value": actual_current, "rationale": rationale}
            )
            continue

        # Confidence floor
        if CONFIDENCE_RANK.get(confidence, 0) < min_conf:
            floor = tuner_cfg.get("min_confidence", "medium")
            rejected.append(
                RejectedChange(
                    knob,
                    proposed,
                    f"confidence '{confidence}' below floor '{floor}'",
                    rationale,
                )
            )
            continue

        # Cooldown
        if knob_in_cooldown(knob, history, cooldown_days, as_of):
            rejected.append(
                RejectedChange(knob, proposed, f"in cooldown ({cooldown_days}d)", rationale)
            )
            continue

        # Snap + clamp
        ladder = ladders[knob]
        final, note = snap_and_clamp(actual_current, proposed, ladder, max_steps)

        if final == actual_current:
            rejected.append(
                RejectedChange(
                    knob,
                    proposed,
                    f"no effective change after guardrails ({note or 'already at value'})",
                    rationale,
                )
            )
            continue

        merged_rationale = rationale
        if note:
            merged_rationale = f"{rationale} [guardrail: {note}]"

        applied.append(
            AppliedChange(
                knob=knob,
                old_value=actual_current,
                new_value=final,
                confidence=confidence,
                rationale=merged_rationale,
            )
        )

    return applied, rejected, no_changes


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------


def _format_knob_table(
    config: dict[str, Any],
    tuner_cfg: dict[str, Any],
    history: list[dict[str, Any]],
    as_of: datetime,
) -> str:
    ladders = tuner_cfg["ladders"]
    cooldown_days = tuner_cfg.get("cooldown_days", 30)
    lines = [
        "| Knob | Current | Legal values (ladder) | In cooldown? |",
        "|------|---------|----------------------|--------------|",
    ]
    for knob, ladder in ladders.items():
        try:
            current = get_knob(config, knob)
        except (KeyError, TypeError):
            current = "?"
        cd = "yes" if knob_in_cooldown(knob, history, cooldown_days, as_of) else "no"
        lines.append(f"| `{knob}` | {current} | {ladder} | {cd} |")
    return "\n".join(lines)


def _format_metrics_block(summary: Any) -> str:
    lines: list[str] = []
    n_scan = len(summary.scanner_runs)
    n_res = len(summary.resolver_runs)
    lines.append(f"- Scanner runs: {n_scan} (success rate {summary.scanner_success_rate}%)")
    lines.append(f"- Resolver runs: {n_res} (success rate {summary.resolver_success_rate}%)")
    lines.append(
        f"- Cumulative fixed: {summary.cumulative_fixed}, "
        f"issues created: {summary.cumulative_issues}, PRs: {summary.cumulative_prs}"
    )
    lines.append(f"- All-time fix rate: {summary.all_time_fix_rate}%")
    if summary.debt_trend:
        lines.append(f"- Raw TODO trend: {summary.debt_trend}")
    if summary.backlog_trend:
        lines.append(f"- Pending backlog trend: {summary.backlog_trend}")
    lines.append("")
    lines.append("Per-resolver-run detail:")
    for r in summary.resolver_runs:
        lines.append(
            f"  - {r.started_at[:10]}: outcome={r.outcome}, "
            f"attempted={r.items_attempted}, fixed={r.items_fixed}, "
            f"issues={r.items_issue_created}, "
            f"budget={r.budget_minutes:.0f}min (used {r.budget_utilization_pct}%), "
            f"duration={r.duration_seconds / 60:.1f}min"
        )
    return "\n".join(lines)


def _format_signals_block(signals: dict[str, Any]) -> str:
    return "\n".join(f"- {k}: {v}" for k, v in signals.items())


def build_tuner_prompt(
    config: dict[str, Any],
    tuner_cfg: dict[str, Any],
    summary: Any,
    signals: dict[str, Any],
    history: list[dict[str, Any]],
    as_of: datetime,
) -> str:
    template = PROMPT_TEMPLATE_PATH.read_text()
    repo_cfg = config["target_repo"]

    cooldown_knobs = [
        knob
        for knob in tuner_cfg["ladders"]
        if knob_in_cooldown(knob, history, tuner_cfg.get("cooldown_days", 30), as_of)
    ]
    cooldown_note = ", ".join(f"`{k}`" for k in cooldown_knobs) if cooldown_knobs else "none"

    return template.format(
        repo_owner=repo_cfg["owner"],
        repo_name=repo_cfg["name"],
        knob_table=_format_knob_table(config, tuner_cfg, history, as_of),
        max_steps=tuner_cfg.get("max_steps_per_change", 1),
        cooldown_note=cooldown_note,
        metrics_block=_format_metrics_block(summary),
        signals_block=_format_signals_block(signals),
    )


# ---------------------------------------------------------------------------
# PR creation
# ---------------------------------------------------------------------------


def open_config_pr(
    config_path: Path,
    applied: list[AppliedChange],
    overall_assessment: str,
    session_url: str,
) -> str | None:
    """Create a branch, commit config + log changes, and open a PR. Returns PR URL or None."""
    branch = f"tuner/{int(time.time())}-config-update"
    summary_line = ", ".join(f"{c.knob} {c.old_value}->{c.new_value}" for c in applied)

    body_lines = [
        "## Automated tuning proposal",
        "",
        "The meta-automation (`src/tuner.py`) reviewed recent scanner/resolver metrics "
        "and proposes the following configuration changes. **Review the reasoning below "
        "and merge to apply, or close to reject.**",
        "",
        f"**Overall assessment**: {overall_assessment}",
        "",
        "### Proposed changes",
        "",
        "| Knob | Old | New | Confidence | Reasoning |",
        "|------|-----|-----|------------|-----------|",
    ]
    for c in applied:
        body_lines.append(
            f"| `{c.knob}` | {c.old_value} | {c.new_value} | {c.confidence} | {c.rationale} |"
        )
    body_lines += [
        "",
        "Decision log: `reports/tuning_log.md`",
    ]
    if session_url:
        body_lines += ["", f"Devin session: {session_url}"]
    body = "\n".join(body_lines)

    try:
        subprocess.run(["git", "checkout", "-b", branch], check=True, capture_output=True)
        subprocess.run(
            ["git", "add", str(config_path), "reports/tuning_log.md", "reports/tuning_log.json"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", f"chore(tuner): {summary_line}"],
            check=True,
            capture_output=True,
        )
        subprocess.run(["git", "push", "-u", "origin", branch], check=True, capture_output=True)
        result = subprocess.run(
            [
                "gh",
                "pr",
                "create",
                "--title",
                f"chore(tuner): tune config ({summary_line})",
                "--body",
                body,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        pr_url = result.stdout.strip()
        logger.info("Opened config PR: %s", pr_url)
        return pr_url
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else exc.stderr
        logger.error("Failed to open config PR: %s", stderr)
        return None


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_tuner(
    config: dict[str, Any],
    config_path: Path,
    api_key: str,
    dry_run: bool = False,
    create_pr: bool = True,
) -> None:
    tuner_cfg = config["tuner"]
    output_dir = Path(config["reports"]["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)

    as_of = datetime.now(timezone.utc)
    timestamp = now_iso()

    # 1. Load reports + analytics
    reports = load_all_reports(output_dir)
    if not reports:
        print("No reports found. Run the scanner first.")
        return
    summary = compute_analytics(reports)

    # 2. Minimum data window check
    history = load_tuning_history(output_dir)
    if len(summary.scanner_runs) < tuner_cfg.get("min_scanner_runs", 3) or len(
        summary.resolver_runs
    ) < tuner_cfg.get("min_resolver_runs", 3):
        reason = (
            f"Insufficient data: {len(summary.scanner_runs)} scanner runs "
            f"(need {tuner_cfg.get('min_scanner_runs', 3)}), "
            f"{len(summary.resolver_runs)} resolver runs "
            f"(need {tuner_cfg.get('min_resolver_runs', 3)})."
        )
        print(reason)
        append_tuning_log(output_dir, timestamp, "", "", [], [], [], skipped_reason=reason)
        print(f"Logged 'no change' decision to {output_dir / 'tuning_log.md'}")
        return

    # 3. Compute deterministic signals
    signals = compute_signals(summary)
    print("Deterministic signals:")
    for k, v in signals.items():
        print(f"  {k}: {v}")
    print()

    # 4. Build prompt + create Devin session
    prompt = build_tuner_prompt(config, tuner_cfg, summary, signals, history, as_of)
    logger.info("Built tuner prompt (%d chars)", len(prompt))

    client = DevinClient(api_key=api_key, org_id=config["devin"]["org_id"])
    auto_repo = "github.com/AshNguyen125/devin_automation"
    session = client.create_session(
        prompt=prompt,
        repos=[auto_repo],
        title="Cadence/Budget Tuner",
        tags=["automation", "tuner", "meta"],
        structured_output_schema=TUNER_OUTPUT_SCHEMA,
        structured_output_required=True,
    )
    print(f"Tuner session created: {session.url}\n")

    result = client.poll_until_done(session.session_id, timeout_seconds=1800)
    structured = result.structured_output

    if not structured:
        reason = "Tuner session produced no structured output."
        logger.warning(reason)
        append_tuning_log(output_dir, timestamp, session.url, "", [], [], [], skipped_reason=reason)
        return

    decisions = structured.get("decisions", [])
    overall = structured.get("overall_assessment", "")

    # 5. Guardrails
    applied, rejected, no_changes = apply_guardrails(decisions, config, tuner_cfg, history, as_of)

    print(f"Overall assessment: {overall}\n")
    print(
        f"Decisions: {len(applied)} applied, {len(rejected)} rejected, {len(no_changes)} no-change"
    )
    for c in applied:
        print(f"  APPLY  {c.knob}: {c.old_value} -> {c.new_value} ({c.confidence})")
    for r in rejected:
        print(f"  REJECT {r.knob} -> {r.proposed_value}: {r.reason}")
    for nc in no_changes:
        print(f"  HOLD   {nc['knob']} @ {nc['current_value']}")
    print()

    # 6. Apply to config file (unless dry-run)
    if applied and not dry_run:
        for c in applied:
            set_knob(config, c.knob, c.new_value)
        _write_config_preserving(config_path, config)
        print(f"Updated {config_path}")

    # 7. Append decision log (skipped in dry-run so cooldown state isn't polluted)
    if dry_run:
        print("[dry-run] Decision log NOT persisted (preview only).")
    else:
        append_tuning_log(
            output_dir,
            timestamp,
            session.url,
            overall,
            applied,
            rejected,
            no_changes,
        )
        print(f"Decision log updated: {output_dir / 'tuning_log.md'}")

    # 8. Open PR
    if applied and not dry_run and create_pr:
        pr_url = open_config_pr(config_path, applied, overall, session.url)
        if pr_url:
            print(f"\nConfig PR opened: {pr_url}")
    elif applied and dry_run:
        print("\n[dry-run] Would open a config.yaml PR with the changes above.")
    elif not applied:
        print("\nNo changes applied; no PR needed.")


def _write_config_preserving(config_path: Path, config: dict[str, Any]) -> None:
    """Write config back to YAML. Uses a round-trip dump (comments are not preserved)."""
    config_path.write_text(yaml.safe_dump(config, sort_keys=False, default_flow_style=False))


def main() -> None:
    parser = argparse.ArgumentParser(description="Tune scanner/resolver cadence and budget")
    parser.add_argument("--config", default="config.yaml", help="Path to config file")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and log decisions but don't edit config or open a PR",
    )
    parser.add_argument(
        "--no-pr",
        action="store_true",
        help="Apply config changes and log, but don't open a PR",
    )
    parser.add_argument("--api-key", default=None, help="Devin API key (default: $DEVIN_API_KEY)")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        logger.error("Config file not found: %s", config_path)
        sys.exit(1)
    with open(config_path) as f:
        config = yaml.safe_load(f)

    api_key = args.api_key or os.environ.get("DEVIN_API_KEY", "")
    if not api_key:
        logger.error("No API key. Set DEVIN_API_KEY or pass --api-key.")
        sys.exit(1)

    run_tuner(
        config,
        config_path,
        api_key,
        dry_run=args.dry_run,
        create_pr=not args.no_pr,
    )


if __name__ == "__main__":
    main()
