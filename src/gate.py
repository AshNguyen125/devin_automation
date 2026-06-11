"""Cadence gate: decide whether an automation is due to run.

Cadence lives in config.yaml (`scanner.cadence_days`, `resolver.cadence_days`,
`tuner.cadence_days`) so the meta-automation can tune it. The CI workflows run on
a frequent fixed cron (daily) and call this gate to decide whether enough days have
passed since the automation last ran. This makes cadence fully config-driven without
rewriting workflow cron expressions.

Exit code 0  -> due to run
Exit code 10 -> not due (skip)

Usage:
    python -m src.gate {scanner|resolver|tuner} [--config config.yaml]
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from src.analytics import load_all_reports

NOT_DUE_EXIT = 10


def last_run_at(automation: str, output_dir: Path) -> datetime | None:
    """Return the most recent start time for the given automation, or None."""
    reports = load_all_reports(output_dir)
    timestamps: list[datetime] = []
    for report in reports:
        for run in report.run_history:
            if run.automation != automation:
                continue
            try:
                timestamps.append(datetime.fromisoformat(run.started_at))
            except (ValueError, TypeError):
                continue
    return max(timestamps) if timestamps else None


def is_due(
    automation: str, config: dict[str, Any], now: datetime | None = None
) -> tuple[bool, str]:
    """Return (due, reason)."""
    now = now or datetime.now(timezone.utc)
    cadence_days = config[automation].get("cadence_days")
    if not cadence_days:
        return True, f"{automation} has no cadence_days configured; running"

    output_dir = Path(config["reports"]["output_dir"])
    last = last_run_at(automation, output_dir)
    if last is None:
        return True, f"{automation} has never run; running"

    days_since = (now - last).total_seconds() / 86400
    if days_since >= cadence_days:
        return (
            True,
            f"{automation} last ran {days_since:.1f}d ago (cadence {cadence_days}d); running",
        )
    return False, f"{automation} last ran {days_since:.1f}d ago (cadence {cadence_days}d); skipping"


def main() -> None:
    parser = argparse.ArgumentParser(description="Cadence gate for automations")
    parser.add_argument("automation", choices=["scanner", "resolver", "tuner"])
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    with open(args.config) as f:
        config = yaml.safe_load(f)

    due, reason = is_due(args.automation, config)
    print(reason)
    sys.exit(0 if due else NOT_DUE_EXIT)


if __name__ == "__main__":
    main()
