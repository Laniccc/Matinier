from __future__ import annotations

import asyncio
import json

from sqlalchemy import func, select

from app.packages import PackageBuilder
from app.persistence.database import Database
from app.persistence.models import DerivedArtifactRecord
from app.processing.clean_script import CleanScriptWorkflow
from app.processing.contracts import ProcessorRegistry
from app.processing.repository import ProcessingJobRepository
from app.processing.runner import ProcessingJobRunner
from app.text_processing.provider import (
    StructuredCompletionRequest,
    StructuredCompletionResult,
)
from tests.test_packages import seed_completed_session


class BlockingProvider:
    provider_name = "fake"
    model = "fake-blocking-v1"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete_structured(
        self,
        request: StructuredCompletionRequest,
    ) -> StructuredCompletionResult:
        self.started.set()
        await self.release.wait()
        items = request.input_payload["items"]
        return StructuredCompletionResult(
            content=json.dumps(
                {
                    "title": "Clean transcript",
                    "sections": [
                        {
                            "source_item_ids": [item["item_id"] for item in items],
                            "clean_text": "Final text.",
                            "notes": [],
                        }
                    ],
                    "warnings": [],
                }
            ),
            finish_reason="stop",
        )


def _registry(provider: BlockingProvider) -> ProcessorRegistry:
    registry = ProcessorRegistry()
    registry.register(
        CleanScriptWorkflow(
            provider,
            max_retries=0,
            max_input_chars=10_000,
            max_items_per_chunk=50,
            retry_delay_seconds=0,
        )
    )
    return registry


def test_restart_recovery_marks_queued_and_running_jobs_failed() -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        with database.session() as db_session:
            package = PackageBuilder(db_session).build_baseline(
                seed_completed_session(db_session)
            )
            repository = ProcessingJobRepository(db_session)
            queued = repository.create_queued(
                package_id=package.package_id,
                artifact_kind="clean_script",
                target_artifact_id=None,
                provider="fake",
                model="fake-blocking-v1",
                options={},
            )
            running = repository.create_queued(
                package_id=package.package_id,
                artifact_kind="clean_script",
                target_artifact_id=None,
                provider="fake",
                model="fake-blocking-v1",
                options={},
            )
            repository.mark_running(running.job_id)
            db_session.commit()

        async def scenario() -> None:
            runner = ProcessingJobRunner(
                database,
                _registry(BlockingProvider()),
            )
            assert await runner.start() == 2
            await runner.stop()

        asyncio.run(scenario())

        with database.session() as db_session:
            repository = ProcessingJobRepository(db_session)
            for job_id in (queued.job_id, running.job_id):
                recovered = repository.get(job_id)
                assert recovered is not None
                assert recovered.status == "failed"
                assert recovered.error_code == "api_process_restarted"
                assert recovered.ended_at is not None
    finally:
        database.dispose()


def test_running_job_can_be_cancelled_without_saving_an_artifact() -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        with database.session() as db_session:
            package = PackageBuilder(db_session).build_baseline(
                seed_completed_session(db_session)
            )
            db_session.commit()

        async def scenario() -> str:
            provider = BlockingProvider()
            runner = ProcessingJobRunner(database, _registry(provider))
            await runner.start()
            job = await runner.submit(
                package_id=package.package_id,
                artifact_kind="clean_script",
                options={},
            )
            await asyncio.wait_for(provider.started.wait(), timeout=1)
            cancelled = await runner.cancel(job.job_id)
            assert cancelled is not None
            assert cancelled.status == "cancelled"
            await asyncio.sleep(0)
            await runner.stop()
            return job.job_id

        job_id = asyncio.run(scenario())

        with database.session() as db_session:
            job = ProcessingJobRepository(db_session).get(job_id)
            assert job is not None
            assert job.status == "cancelled"
            assert db_session.scalar(
                select(func.count()).select_from(DerivedArtifactRecord)
            ) == 0
    finally:
        database.dispose()
