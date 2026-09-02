from __future__ import annotations

from app.persistence.database import Database
from app.processing.chapter_outline import ChapterOutlineWorkflow
from app.processing.clean_script import CleanScriptWorkflow
from app.processing.contracts import ProcessorRegistry
from app.processing.refined_translation import RefinedTranslationWorkflow
from app.processing.runner import ProcessingJobRunner
from app.processing.summary import SummaryWorkflow
from app.processing.timeline_fact_review import TimelineFactReviewWorkflow
from app.settings import Settings
from app.text_processing.deepseek_provider import DeepSeekCompletionProvider


def build_processor_registry(settings: Settings) -> ProcessorRegistry:
    provider = DeepSeekCompletionProvider(
        api_key=settings.deepseek_api_key,
        base_url=str(settings.deepseek_base_url),
        model=settings.deepseek_model,
        timeout_seconds=settings.deepseek_request_timeout_seconds,
        temperature=settings.deepseek_temperature,
        max_output_tokens=settings.deepseek_max_output_tokens,
    )
    registry = ProcessorRegistry()
    registry.register(
        CleanScriptWorkflow(
            provider,
            max_retries=settings.deepseek_max_retries,
            max_input_chars=settings.deepseek_max_input_chars,
            max_items_per_chunk=settings.deepseek_max_segments_per_chunk,
        )
    )
    registry.register(
        RefinedTranslationWorkflow(
            provider,
            max_retries=settings.deepseek_max_retries,
            max_input_chars=settings.deepseek_max_input_chars,
            max_items_per_chunk=settings.deepseek_max_segments_per_chunk,
        )
    )
    registry.register(
        SummaryWorkflow(
            provider,
            max_retries=settings.deepseek_max_retries,
            max_input_chars=settings.deepseek_max_input_chars,
            max_items_per_chunk=settings.deepseek_max_segments_per_chunk,
        )
    )
    registry.register(
        ChapterOutlineWorkflow(
            provider,
            max_retries=settings.deepseek_max_retries,
            max_input_chars=settings.deepseek_max_input_chars,
            max_items_per_chunk=settings.deepseek_max_segments_per_chunk,
        )
    )
    registry.register(
        TimelineFactReviewWorkflow(
            provider,
            max_retries=settings.deepseek_max_retries,
            max_input_chars=settings.deepseek_max_input_chars,
            max_items_per_chunk=settings.deepseek_max_segments_per_chunk,
        )
    )
    return registry


def build_processing_job_runner(
    settings: Settings,
    database: Database,
) -> ProcessingJobRunner:
    return ProcessingJobRunner(
        database,
        build_processor_registry(settings),
        concurrency=1,
    )
