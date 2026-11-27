"""Grounded question answering, independent from folder indexing."""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from functools import cache
from pathlib import Path

from . import backends, prompts
from .config import Config
from .library import card, object_inventory, overview, query_embedding, search
from .store import Store


def validate_response(data, candidates):
    if not isinstance(data.get("answer"), str) or not isinstance(data.get("evidence_ids"), list):
        raise ValueError("Answer model returned an invalid structured response")
    if any(not isinstance(i, str) or i not in candidates for i in data["evidence_ids"]):
        raise ValueError("Answer model cited evidence that was not supplied")
    # A sampled index cannot justify universal claims that an entire video lacks
    # something. Models sometimes append such a claim after quoting positive evidence.
    absolute_absence = (
        r"\b(?:видео|ролик\w*|запис\w*|архив\w*)\s+не\s+(?:содерж\w*|показ\w*|име\w*)",
        r"\b(?:video|footage|recording|archive)\s+(?:contains\s+no|has\s+no|does\s+not|doesn't|never)",
    )
    if any(re.search(pattern, data["answer"], re.IGNORECASE) for pattern in absolute_absence):
        raise ValueError("Unsupported claim of absence in an entire source; describe retrieved evidence only")
    words = re.findall(r"\w+", data["answer"].casefold())
    phrases = Counter(tuple(words[i : i + 6]) for i in range(max(0, len(words) - 5)))
    if any(count >= 3 for count in phrases.values()):
        raise ValueError("Repetitive answer; use a short answer without repeated phrases")


def answer(
    store: Store,
    config: Config,
    question: str,
    lexical_only=False,
    generator_factory=backends.answer_generator,
    video_id=None,
):
    if video_id is None:
        normalized_question = unicodedata.normalize("NFC", question).casefold()
        mentioned = [
            r["id"]
            for r in store.db.execute("SELECT id,path FROM videos WHERE present=1")
            if unicodedata.normalize("NFC", Path(r["path"]).name).casefold() in normalized_question
        ]
        if len(mentioned) == 1:
            video_id = mentioned[0]
    # Mixed retrieval and per-layer quotas use the same normalized query. Keep
    # only its small vector for this request, never a loaded model across calls.
    vector = cache(lambda: query_embedding(config, question, backends.embedder))
    results = search(
        store,
        config,
        question,
        top_k=6,
        video_id=video_id,
        lexical_only=lexical_only,
        query_vector_factory=vector,
    )
    # One channel (e.g. repeated subtitles) must not crowd speech and visuals out
    # of a mixed question. Keep a small evidence quota for each completed layer.
    seen = {hit["id"] for hit in results["hits"]}
    for source in ("speech", "visual", "ocr"):
        layer = search(
            store,
            config,
            question,
            top_k=2,
            video_id=video_id,
            source=source,
            lexical_only=lexical_only,
            query_vector_factory=vector,
        )
        for hit in layer["hits"]:
            if hit["id"] not in seen:
                results["hits"].append(hit)
                seen.add(hit["id"])
    library = overview(store, limit=10, video_id=video_id)
    inventory = object_inventory(store, video_id=video_id, limit=15)
    # Expand rare candidates to actual evidence rather than answering from labels alone.
    rare = []
    exploratory = re.search(
        r"unusual|interesting|rare|highlights|необыч|интересн|редк", question, re.IGNORECASE
    )
    for obj in inventory["objects"][: 8 if exploratory else 2]:
        rare.append(card(store.item(obj["example_ids"][0])))
    evidence = {
        "retrieved": results,
        "library_overview": {
            "total": library["total"],
            "videos": [
                {"video_id": v["video_id"], "file": Path(v["path"]).name, "visual_summary": v["summary"]}
                for v in library["videos"]
            ],
        },
        "rare_object_examples": rare,
        "inventory": inventory,
        "coverage": "The question uses retrieved evidence, not a fresh exhaustive viewing. "
        "At most 10 video summaries are included; use MCP pagination for larger libraries.",
    }
    if not results["hits"] and not rare and not any(v["summary"] for v in library["videos"]):
        return {
            "answer": "В индексе пока нет подходящих наблюдений. Проверьте статус индексации.",
            "evidence": evidence,
        }
    candidates = {n["id"]: n for n in [*results["hits"], *rare]}
    for video in library["videos"]:
        if video["summary"]:
            candidates[video["summary"]["id"]] = {**video["summary"], "path": video["path"]}
    schema = prompts.object_schema(
        {
            "answer": prompts.short_string(1400),
            "evidence_ids": {
                "type": "array",
                "maxItems": 8,
                "items": {"type": "string", "enum": list(candidates)},
            },
        }
    )
    russian_question = bool(re.search("[А-Яа-яЁё]", question)) and not re.search("[\u3400-\u9fff]", question)
    if russian_question:
        # Small multilingual models occasionally substitute Chinese technical
        # terms. Constrain the output script as well as asking for Russian prose.
        schema["properties"]["answer"]["pattern"] = "^[^\u3400-\u9fff]*$"
    config.configure_cache()
    model = generator_factory(config)
    try:
        prompt = (
            prompts.ANSWER
            + "\nEVIDENCE:\n"
            + json.dumps(evidence, ensure_ascii=False)
            + "\nQUESTION:\n"
            + question
        )
        if russian_question:
            prompt += "\nWrite Russian prose only. Translate technical terms into Russian; keep source quotes as quotes."
        for attempt in range(2):
            try:
                response = model.generate(
                    prompt, max_tokens=config.answer.max_tokens * (attempt + 1), schema=schema
                )
                data = backends.json_object(response)
                validate_response(data, candidates)
                break
            except ValueError as exc:
                if attempt:
                    raise
                prompt += (
                    f"\nRewrite your answer. Validation error: {exc}. "
                    "Use a short factual description with valid sources. Never claim the entire video "
                    "lacks an event; say only what the supplied observations show. "
                    "Keep speech, writing and visual facts separate."
                )
    finally:
        model.close()
    cited = list(dict.fromkeys(data["evidence_ids"]))
    sources = []
    for item_id in cited:
        if item_id in candidates:
            node = candidates[item_id]
            sources.append(
                {
                    "id": item_id,
                    "file": Path(node["path"]).name,
                    "start": timecode(node["start_s"]),
                    "end": timecode(node["end_s"]),
                }
            )
    return {"answer": data["answer"], "sources": sources, "evidence": evidence}


def timecode(seconds):
    hours, remainder = divmod(round(seconds * 1000), 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    sec, ms = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{sec:02}.{ms:03}"
