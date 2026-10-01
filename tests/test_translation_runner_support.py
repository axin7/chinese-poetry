import pytest

from translation_pipeline.runner import (
    AdaptiveConcurrencyController,
    ProgressReporter,
    _is_load_failure,
)


def test_progress_reporter_tracks_cpu_and_throughput():
    reporter = ProgressReporter()
    reporter.sample(
        wall_time=100.0,
        cpu_time=10.0,
        processed=100,
        failed=5,
        current_limit=256,
        in_flight=128,
        buffered=512,
    )

    metrics = reporter.sample(
        wall_time=105.0,
        cpu_time=12.5,
        processed=140,
        failed=7,
        current_limit=320,
        in_flight=200,
        buffered=300,
    )

    assert metrics["cpu_percent"] == pytest.approx(50.0)
    assert metrics["records_per_second"] == pytest.approx(8.0)
    assert metrics["failed_per_second"] == pytest.approx(0.4)
    assert metrics["current_limit"] == 320
    assert metrics["in_flight"] == 200
    assert metrics["buffered"] == 300


def test_adaptive_concurrency_controller_scales_up_on_fast_successes():
    controller = AdaptiveConcurrencyController(
        max_limit=128,
        request_timeout=30,
        initial_limit=32,
        min_limit=8,
    )

    for _ in range(32):
        controller.observe(duration_seconds=1.2, failed=False, timed_out=False)

    assert controller.current_limit > 32


def test_load_failure_classification_ignores_content_quality_errors():
    assert _is_load_failure("Error code: 429 - 速率限制") is True
    assert _is_load_failure("API connection error") is True
    assert _is_load_failure("译文条数与原文不一致") is False
    assert _is_load_failure("译文与原文完全相同") is False


def test_adaptive_concurrency_controller_scales_down_on_timeouts():
    controller = AdaptiveConcurrencyController(
        max_limit=128,
        request_timeout=30,
        initial_limit=64,
        min_limit=8,
    )

    for _ in range(24):
        controller.observe(duration_seconds=4.0, failed=False, timed_out=False)
    for _ in range(8):
        controller.observe(duration_seconds=30.0, failed=True, timed_out=True)

    assert controller.current_limit < 64
