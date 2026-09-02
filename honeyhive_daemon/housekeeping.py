"""Periodic retention work for daemon logs and local state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .config import (
    get_log_backup_count,
    get_log_max_bytes,
    get_spool_max_events,
    get_state_retention_days,
)
from .state import (
    log_message,
    prune_finished_sessions,
    rotate_log,
    trim_spool,
)


HOUSEKEEPING_INTERVAL_SEC = 15 * 60


@dataclass
class HousekeepingReport:
    """Outcome of one housekeeping pass."""

    log_rotated: bool = False
    spool_events_dropped: int = 0
    sessions_pruned: int = 0

    def did_work(self) -> bool:
        """Return True when the pass changed anything on disk."""
        return bool(
            self.log_rotated or self.spool_events_dropped or self.sessions_pruned
        )


def run_housekeeping(*, now_ms: int | None = None) -> HousekeepingReport:
    """Rotate the daemon log, trim the spool, and prune finished session state."""
    if now_ms is None:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    report = HousekeepingReport(
        log_rotated=rotate_log(
            max_bytes=get_log_max_bytes(), backups=get_log_backup_count()
        ),
        spool_events_dropped=trim_spool(get_spool_max_events()),
        sessions_pruned=prune_finished_sessions(
            get_state_retention_days() * 24 * 60 * 60 * 1000, now_ms=now_ms
        ),
    )

    if report.did_work():
        log_message(
            "housekeeping "
            f"log_rotated={report.log_rotated} "
            f"spool_dropped={report.spool_events_dropped} "
            f"sessions_pruned={report.sessions_pruned}"
        )
    return report
