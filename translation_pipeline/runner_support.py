from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass

logger = logging.getLogger(__name__)
ADAPTIVE_WINDOW_SIZE = 32
LOAD_FAILURE_MARKERS = (
    "429",
    "rate limit",
    "connection error",
    "temporarily unavailable",
    "速率限制",
)


def _is_load_failure(error_text: str) -> bool:
    lowered = error_text.lower()
    return any(marker in lowered for marker in LOAD_FAILURE_MARKERS)


@dataclass(frozen=True)
class RecordProcessingResult:
    duration_seconds: float
    failed: bool
    timed_out: bool


class AdaptiveConcurrencyController:
    def __init__(
        self,
        max_limit: int,
        request_timeout: int,
        initial_limit: int | None = None,
        min_limit: int = 8,
    ):
        self.max_limit = max_limit
        self.request_timeout = request_timeout
        self.min_limit = min(min_limit, max_limit)
        default_initial = min(max_limit, max(32, min(256, max_limit)))
        self.current_limit = max(self.min_limit, initial_limit or default_initial)
        self._recent_results: deque[RecordProcessingResult] = deque(
            maxlen=ADAPTIVE_WINDOW_SIZE
        )

    def observe(self, duration_seconds: float, failed: bool, timed_out: bool) -> None:
        self._recent_results.append(
            RecordProcessingResult(
                duration_seconds=duration_seconds,
                failed=failed,
                timed_out=timed_out,
            )
        )
        if len(self._recent_results) >= ADAPTIVE_WINDOW_SIZE:
            self._rebalance()

    def _rebalance(self) -> None:
        results = list(self._recent_results)
        total = len(results)
        timeout_count = sum(1 for item in results if item.timed_out)
        failed_count = sum(1 for item in results if item.failed)
        avg_duration = sum(item.duration_seconds for item in results) / total
        timeout_rate = timeout_count / total
        failure_rate = failed_count / total
        next_limit = self._next_limit(timeout_rate, failure_rate, avg_duration)
        if next_limit != self.current_limit:
            logger.info(
                "自适应并发调整: %s -> %s "
                "(avg=%.2fs, timeout_rate=%.2f, failure_rate=%.2f)",
                self.current_limit,
                next_limit,
                avg_duration,
                timeout_rate,
                failure_rate,
            )
            self.current_limit = next_limit
            self._recent_results.clear()

    def _next_limit(
        self,
        timeout_rate: float,
        failure_rate: float,
        avg_duration: float,
    ) -> int:
        if timeout_rate >= 0.10 or failure_rate >= 0.20:
            return max(self.min_limit, int(self.current_limit * 0.7))
        if (
            timeout_rate == 0
            and failure_rate <= 0.05
            and avg_duration <= self.request_timeout * 0.30
        ):
            return min(
                self.max_limit,
                max(self.current_limit + 1, int(self.current_limit * 1.2)),
            )
        return self.current_limit


class ProgressReporter:
    def __init__(self):
        self._last_wall_time: float | None = None
        self._last_cpu_time: float | None = None
        self._last_processed = 0
        self._last_failed = 0

    def sample(
        self,
        wall_time: float,
        cpu_time: float,
        processed: int,
        failed: int,
        current_limit: int,
        in_flight: int,
        buffered: int,
    ) -> dict[str, float | int]:
        if self._last_wall_time is None or self._last_cpu_time is None:
            self._last_wall_time = wall_time
            self._last_cpu_time = cpu_time
            self._last_processed = processed
            self._last_failed = failed
            return self._metrics(0.0, 0.0, 0.0, current_limit, in_flight, buffered)

        delta_wall = max(wall_time - self._last_wall_time, 1e-6)
        delta_cpu = max(cpu_time - self._last_cpu_time, 0.0)
        delta_processed = processed - self._last_processed
        delta_failed = failed - self._last_failed
        self._last_wall_time = wall_time
        self._last_cpu_time = cpu_time
        self._last_processed = processed
        self._last_failed = failed
        return self._metrics(
            (delta_cpu / delta_wall) * 100,
            delta_processed / delta_wall,
            delta_failed / delta_wall,
            current_limit,
            in_flight,
            buffered,
        )

    def _metrics(
        self,
        cpu_percent: float,
        records_per_second: float,
        failed_per_second: float,
        current_limit: int,
        in_flight: int,
        buffered: int,
    ) -> dict[str, float | int]:
        return {
            "cpu_percent": cpu_percent,
            "records_per_second": records_per_second,
            "failed_per_second": failed_per_second,
            "current_limit": current_limit,
            "in_flight": in_flight,
            "buffered": buffered,
        }


@dataclass(frozen=True)
class TableSpec:
    table_name: str
    tag_field: str
    display_name: str
