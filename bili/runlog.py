"""Small helpers for run lifecycle and health reporting."""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone

BJ = timezone(timedelta(hours=8))


def now_ts() -> int:
    return int(time.time())


def new_run_id() -> str:
    return f"run-{now_ts()}-{uuid.uuid4().hex[:8]}"


def today_bj() -> str:
    return datetime.now(BJ).strftime("%Y-%m-%d")


def next_day_start_bj() -> int:
    tomorrow = (datetime.now(BJ) + timedelta(days=1)).replace(
        hour=0, minute=5, second=0, microsecond=0
    )
    return int(tomorrow.timestamp())


def today_start_ts() -> int:
    start = datetime.now(BJ).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp())


def fmt_ts(ts: int) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts), BJ).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return ""
