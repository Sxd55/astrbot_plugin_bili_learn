"""B 站接口节流与全局冷却。

间隔带随机抖动，避免固定频率被风控；命中限流后全局冷却，冷却期内所有请求统一等待。
"""

from __future__ import annotations

import asyncio
import random
import time


class BiliThrottle:
    def __init__(self, min_gap: float = 3.0):
        try:
            gap = float(min_gap if min_gap is not None else 3.0)
        except (TypeError, ValueError):
            gap = 3.0
        self.min_gap = min(30.0, max(0.5, gap))
        self._last = 0.0
        self._lock = asyncio.Lock()
        self._cooldown_until = 0.0

    def cooldown_remaining(self) -> float:
        return max(0.0, self._cooldown_until - time.monotonic())

    def trigger_cooldown(self, minimum: float = 90.0, maximum: float = 180.0) -> float:
        now = time.monotonic()
        seconds = random.uniform(minimum, maximum)
        if now + seconds > self._cooldown_until:
            self._cooldown_until = now + seconds
        return self.cooldown_remaining()

    async def wait(self) -> None:
        async with self._lock:
            remain = self.cooldown_remaining()
            if remain > 0:
                await asyncio.sleep(remain + 0.5)
            jitter = random.uniform(0.0, min(1.0, self.min_gap))
            gap = self.min_gap + jitter - (time.monotonic() - self._last)
            if gap > 0:
                await asyncio.sleep(gap)
            self._last = time.monotonic()
