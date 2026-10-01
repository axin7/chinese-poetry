from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from pathlib import Path
from typing import List

from .client import TranslationClient
from .config import TranslationConfig
from .database import PendingRecord, TranslationDatabase
from .response import TranslationOutcome
from .runner_support import (
    AdaptiveConcurrencyController,
    ProgressReporter,
    RecordProcessingResult,
    TableSpec,
    _is_load_failure,
)

logger = logging.getLogger(__name__)
FETCH_WINDOW_MULTIPLIER = 4
PROGRESS_LOG_INTERVAL_SECONDS = 15


class TranslationRunner:
    def __init__(
        self,
        db_path: str | Path,
        config_path: str | Path,
        config: TranslationConfig,
        batch_size: int,
        selected_tables: list[str] | None = None,
        max_records: int | None = None,
    ):
        self.db_path = Path(db_path)
        self.config_path = Path(config_path)
        self.config = config
        self.batch_size = batch_size
        self.selected_tables = {name.strip() for name in (selected_tables or []) if name.strip()}
        self.max_records = max_records
        self.database = TranslationDatabase(
            self.db_path,
            write_batch_size=self.config.db_write_batch_size,
        )
        self.client = TranslationClient(config)
        self.table_specs: List[TableSpec] = []
        self.total_dispatched = 0
        self.total_processed = 0
        self.total_failed = 0
        self.total_skipped = 0

    async def run(self) -> None:
        start_time = time.time()
        run_error: BaseException | None = None
        try:
            self.database.connect()
            await self.client.open()
            self.table_specs = self._load_table_specs()
            self._prepare_tables()
            for table_spec in self.table_specs:
                await self._run_table(table_spec)
        except BaseException as exc:
            run_error = exc
        finally:
            if run_error is not None:
                self._rollback_after_failure()
            close_error = await self._close_resources()
            if run_error is not None:
                raise run_error
            if close_error is not None:
                raise close_error

        logger.info(
            "翻译任务结束，成功 %s 条，永久跳过 %s 条，"
            "失败尝试 %s 次，耗时 %.2f 秒",
            self.total_processed,
            self.total_skipped,
            self.total_failed,
            time.time() - start_time,
        )

    def _rollback_after_failure(self) -> None:
        try:
            self.database.rollback()
        except Exception:
            logger.exception("回滚未提交的翻译状态失败")

    async def _close_resources(self) -> Exception | None:
        close_error: Exception | None = None
        try:
            self.database.close()
        except Exception as exc:
            close_error = exc
            logger.exception("关闭数据库连接失败")
        try:
            await self.client.close()
        except Exception as exc:
            if close_error is None:
                close_error = exc
            logger.exception("关闭翻译客户端失败")
        return close_error

    def _load_table_specs(self) -> List[TableSpec]:
        with self.config_path.open("r", encoding="utf-8") as file_obj:
            config_data = json.load(file_obj)
        datasets = config_data.get("datasets", {})
        return [
            TableSpec(
                table_name=dataset_key.replace("-", "_"),
                tag_field=dataset_value.get("tag", "paragraphs"),
                display_name=dataset_value.get("name", dataset_key),
            )
            for dataset_key, dataset_value in datasets.items()
        ]

    def _prepare_tables(self) -> None:
        existing_tables = set(self.database.get_all_tables())
        configured_tables = {spec.table_name for spec in self.table_specs}
        self._validate_selected_tables(configured_tables, existing_tables)
        valid_specs = [
            spec
            for spec in self.table_specs
            if spec.table_name in existing_tables
        ]
        if self.selected_tables:
            valid_specs = [
                spec for spec in valid_specs if spec.table_name in self.selected_tables
            ]
        for spec in valid_specs:
            self.database.ensure_translation_fields(spec.table_name)
        self.database.reset_processing_records(
            [spec.table_name for spec in valid_specs]
        )
        self.table_specs = valid_specs

    def _validate_selected_tables(
        self,
        configured_tables: set[str],
        existing_tables: set[str],
    ) -> None:
        unknown_tables = self.selected_tables - configured_tables
        if unknown_tables:
            names = ", ".join(sorted(unknown_tables))
            raise ValueError(f"--tables 包含未配置的表: {names}")

        missing_tables = self.selected_tables - existing_tables
        if missing_tables:
            names = ", ".join(sorted(missing_tables))
            raise ValueError(f"--tables 指定的表不存在于数据库: {names}")

    async def _run_table(self, table_spec: TableSpec) -> None:
        if self.max_records is not None and self.total_dispatched >= self.max_records:
            return

        pending_count = self._log_pending_count(table_spec)
        if pending_count == 0:
            return

        controller = AdaptiveConcurrencyController(
            max_limit=self.config.max_concurrency,
            request_timeout=self.config.request_timeout,
        )
        progress_reporter = ProgressReporter()
        fetch_window_size = max(
            self.batch_size,
            self.config.max_concurrency * FETCH_WINDOW_MULTIPLIER,
        )
        buffered_records = deque()
        in_flight_tasks: set[asyncio.Task[RecordProcessingResult]] = set()
        progress_task = self._start_progress_task(
            table_spec,
            controller,
            buffered_records,
            in_flight_tasks,
            progress_reporter,
        )

        try:
            await self._process_table_records(
                buffered_records,
                in_flight_tasks,
                table_spec,
                controller,
                fetch_window_size,
            )
        except (Exception, asyncio.CancelledError):
            await self._cancel_in_flight_tasks(in_flight_tasks)
            self.database.reset_processing_records([table_spec.table_name])
            raise
        finally:
            progress_task.cancel()
            await asyncio.gather(progress_task, return_exceptions=True)

        logger.info("表=%s 处理完成", table_spec.table_name)

    def _log_pending_count(self, table_spec: TableSpec) -> int:
        pending_count = self.database.count_pending_records(
            table_spec.table_name,
            self.config.max_record_failures,
        )
        logger.info(
            "开始处理表=%s（%s），待处理 %s 条",
            table_spec.table_name,
            table_spec.display_name,
            pending_count,
        )
        return pending_count

    def _start_progress_task(
        self,
        table_spec: TableSpec,
        controller: AdaptiveConcurrencyController,
        buffered_records: deque[PendingRecord],
        in_flight_tasks: set[asyncio.Task[RecordProcessingResult]],
        progress_reporter: ProgressReporter,
    ) -> asyncio.Task[None]:
        return asyncio.create_task(
            self._log_progress_periodically(
                table_spec=table_spec,
                controller=controller,
                buffered_records=buffered_records,
                in_flight_tasks=in_flight_tasks,
                progress_reporter=progress_reporter,
            )
        )

    async def _process_table_records(
        self,
        buffered_records: deque[PendingRecord],
        in_flight_tasks: set[asyncio.Task[RecordProcessingResult]],
        table_spec: TableSpec,
        controller: AdaptiveConcurrencyController,
        fetch_window_size: int,
    ) -> None:
        last_dispatched_id = 0
        while True:
            last_dispatched_id = await self._fill_record_buffer(
                buffered_records,
                table_spec,
                fetch_window_size,
                last_dispatched_id,
            )
            self._start_buffered_tasks(buffered_records, in_flight_tasks, controller)
            if not in_flight_tasks:
                return
            await self._observe_completed_tasks(in_flight_tasks, controller)
            self.database.flush()

    def _start_buffered_tasks(
        self,
        buffered_records: deque[PendingRecord],
        in_flight_tasks: set[asyncio.Task[RecordProcessingResult]],
        controller: AdaptiveConcurrencyController,
    ) -> None:
        while buffered_records and len(in_flight_tasks) < controller.current_limit:
            record = buffered_records.popleft()
            in_flight_tasks.add(asyncio.create_task(self._process_record(record)))

    async def _observe_completed_tasks(
        self,
        in_flight_tasks: set[asyncio.Task[RecordProcessingResult]],
        controller: AdaptiveConcurrencyController,
    ) -> None:
        done_tasks, _ = await asyncio.wait(
            in_flight_tasks,
            return_when=asyncio.FIRST_COMPLETED,
        )
        in_flight_tasks.difference_update(done_tasks)
        for task in done_tasks:
            result = task.result()
            controller.observe(
                duration_seconds=result.duration_seconds,
                failed=result.failed,
                timed_out=result.timed_out,
            )

    async def _fill_record_buffer(
        self,
        buffered_records: deque[PendingRecord],
        table_spec: TableSpec,
        fetch_window_size: int,
        after_record_id: int,
    ) -> int:
        if len(buffered_records) >= fetch_window_size:
            return after_record_id

        remaining_limit = None
        if self.max_records is not None:
            remaining_limit = self.max_records - self.total_dispatched
            if remaining_limit <= 0:
                return after_record_id

        current_limit = fetch_window_size - len(buffered_records)
        if remaining_limit is not None:
            current_limit = min(current_limit, remaining_limit)
        if current_limit <= 0:
            return after_record_id

        records = self.database.get_pending_records(
            table_spec.table_name,
            table_spec.tag_field,
            current_limit,
            self.config.max_record_failures,
            after_record_id,
        )
        if not records:
            return after_record_id

        for record in records:
            self.database.mark_processing(table_spec.table_name, record.record_id)
        self.database.flush()

        buffered_records.extend(records)
        self.total_dispatched += len(records)
        return records[-1].record_id

    async def _process_record(self, record: PendingRecord) -> RecordProcessingResult:
        started_at = time.perf_counter()
        if not record.content:
            return self._skip_empty_record(record, started_at)

        try:
            outcome = await self.client.translate(
                record.content,
                record.table_name,
                record.record_id,
            )
            return self._store_outcome(record, outcome, started_at)
        except Exception as exc:
            return self._store_failure(record, exc, started_at)

    def _skip_empty_record(
        self,
        record: PendingRecord,
        started_at: float,
    ) -> RecordProcessingResult:
        self.database.mark_skipped(
            record.table_name,
            record.record_id,
            "内容为空或解析失败，已跳过",
        )
        self.total_skipped += 1
        return RecordProcessingResult(
            duration_seconds=time.perf_counter() - started_at,
            failed=False,
            timed_out=False,
        )

    def _store_outcome(
        self,
        record: PendingRecord,
        outcome: TranslationOutcome,
        started_at: float,
    ) -> RecordProcessingResult:
        if outcome.status == "done":
            self.database.mark_done(
                record.table_name,
                record.record_id,
                outcome.translation,
                outcome.interpretation,
            )
            self.total_processed += 1
        else:
            self.database.mark_skipped(
                record.table_name,
                record.record_id,
                outcome.reason or "已跳过",
            )
            self.total_skipped += 1
        return RecordProcessingResult(
            duration_seconds=time.perf_counter() - started_at,
            failed=False,
            timed_out=False,
        )

    def _store_failure(
        self,
        record: PendingRecord,
        error: Exception,
        started_at: float,
    ) -> RecordProcessingResult:
        error_text = str(error)
        self.database.mark_failed(
            record.table_name,
            record.record_id,
            error_text,
            max_record_failures=self.config.max_record_failures,
        )
        self.total_failed += 1
        lowered_error = error_text.lower()
        timed_out = "timed out" in lowered_error or "timeout" in lowered_error
        return RecordProcessingResult(
            duration_seconds=time.perf_counter() - started_at,
            failed=timed_out or _is_load_failure(error_text),
            timed_out=timed_out,
        )

    async def _cancel_in_flight_tasks(
        self,
        tasks: set[asyncio.Task[RecordProcessingResult]],
    ) -> None:
        if not tasks:
            return
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _log_progress_periodically(
        self,
        table_spec: TableSpec,
        controller: AdaptiveConcurrencyController,
        buffered_records: deque[PendingRecord],
        in_flight_tasks: set[asyncio.Task[RecordProcessingResult]],
        progress_reporter: ProgressReporter,
    ) -> None:
        try:
            while True:
                await asyncio.sleep(PROGRESS_LOG_INTERVAL_SECONDS)
                metrics = progress_reporter.sample(
                    wall_time=time.perf_counter(),
                    cpu_time=time.process_time(),
                    processed=self.total_processed,
                    failed=self.total_failed,
                    current_limit=controller.current_limit,
                    in_flight=len(in_flight_tasks),
                    buffered=len(buffered_records),
                )
                logger.info(
                    "运行监控: 表=%s cpu=%.1f%% 速率=%.2f条/s "
                    "失败速率=%.2f条/s 并发上限=%s in_flight=%s "
                    "buffer=%s 已成功=%s 已失败=%s",
                    table_spec.table_name,
                    metrics["cpu_percent"],
                    metrics["records_per_second"],
                    metrics["failed_per_second"],
                    metrics["current_limit"],
                    metrics["in_flight"],
                    metrics["buffered"],
                    self.total_processed,
                    self.total_failed,
                )
        except asyncio.CancelledError:
            return
