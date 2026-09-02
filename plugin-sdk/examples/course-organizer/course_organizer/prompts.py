from __future__ import annotations

from .models import FINAL_CATEGORIES


def final_categories_prompt() -> str:
    categories = "\n".join(f"- {name}" for name in FINAL_CATEGORIES)
    return (
        "Return one JSON object with exactly one top-level key named items. "
        "items must be an array; use {\"items\":[]} when nothing is supported. "
        "Every item must contain exactly these keys: id, category, topic_path, "
        "title, statement, explanation, evidence_item_ids, related_item_ids, "
        "confirmation_status. id/title/statement/explanation must be non-empty "
        "strings. topic_path and evidence_item_ids must be non-empty arrays of "
        "unique strings. related_item_ids must be an array of unique IDs that "
        "also occur in this response; use [] when uncertain. confirmation_status "
        "must be confirmed or needs_confirmation. Write natural-language fields "
        "in input_payload.output_language. Only retain statements supported by "
        "the supplied evidence IDs, and copy those IDs exactly. Classify every "
        "retained knowledge item into exactly one fixed category:\n"
        f"{categories}\n"
        "Do not invent an evidence ID, source segment, timestamp, or fact. "
        "Keep uncertain or contradictory material needs_confirmation. Do not "
        "add markdown, code fences, commentary, or any extra JSON field. "
        "Keep titles short, statements to one sentence, and explanations to at "
        "most two short sentences. Merge repeated points within this batch."
    )


def realtime_notes_prompt() -> str:
    return (
        "Return one JSON object with exactly one top-level key named notes. "
        "notes must be an array; use {\"notes\":[]} when the window contains no "
        "course knowledge. Every note must contain exactly these keys: id, "
        "note_type, title, body, evidence_item_ids, confidence_status, language, "
        "related_note_ids. id/title/body/language must be non-empty strings. "
        "note_type must be knowledge_candidate, example, transition, background, "
        "or needs_confirmation. evidence_item_ids must be a non-empty array of "
        "unique IDs copied exactly from the supplied items. related_note_ids must "
        "reference IDs in this response; use [] when uncertain. confidence_status "
        "must be confirmed or needs_confirmation, and uncertain evidence cannot "
        "be confirmed. language must equal input_payload.output_language. Extract "
        "sparse course notes from the current bounded window. "
        "Prefer definitions, mechanisms, comparisons, procedures, formulas, "
        "examples, conclusions, and teacher emphasis. Exclude filler and course "
        "administration. Cite only supplied evidence IDs and preserve uncertainty. "
        "Do not add markdown, code fences, commentary, or extra JSON fields."
        " Return at most three concise notes per window; keep each body to at "
        "most two short sentences and merge repeated points."
    )


__all__ = ["final_categories_prompt", "realtime_notes_prompt"]
