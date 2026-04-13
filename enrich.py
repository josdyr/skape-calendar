"""Call GitHub Models (OpenAI-compatible) to extract structured metadata for events."""
from __future__ import annotations

import json
import os
import sys
from typing import Optional

from openai import OpenAI, OpenAIError
from pydantic import BaseModel, Field

MODEL = "openai/gpt-4o-mini"
BASE_URL = "https://models.github.ai/inference"
BATCH_SIZE = 5  # events per request; stays under GitHub Models' 8K input cap


SYSTEM = """You extract structured metadata from Norwegian course/webinar event pages from skape.no.

Pages use emoji markers as field labels:
- 📅 date
- 🕓 time
- 🗺️ physical venue/address (absence usually means a digital event)
- ⌛️ Påmeldingsfrist = registration deadline
- 💰 price (NOK)
- 📢 Arrangør = organizer
- 💬 Kursholder = instructor
- 🌍 / Språk = language
- 👥 audience / Målgruppe

Rules:
- If no physical venue is found, treat the event as digital (webinar): is_digital=true, location_physical=null.
- "Gratis"/"Free"/"GRATIS" → price_nok=0. A number like "300,-" → price_nok=300.
- registration_deadline: return ISO 8601 date (YYYY-MM-DD). If only day+month is given, assume the event's year. Return null if absent.
- summary: one or two sentences in English describing what attendees will learn or do. Neutral, concrete.
- Preserve Norwegian text verbatim in location, organizer, instructor — do not translate.
- language: use English language name ("Norwegian", "English", "Norwegian/English").
- Never fabricate data. Use null for anything not clearly stated.
- Return exactly one entry per event, with the integer index you were given.

Respond ONLY with a JSON object of shape:
{"events": [{"index": <int>, "metadata": {"summary": <str>, "is_digital": <bool>, "location_physical": <str|null>, "registration_deadline": <str|null>, "price_nok": <number|null>, "organizer": <str|null>, "instructor": <str|null>, "language": <str|null>, "audience": <str|null>, "registration_url": <str|null>}}]}
"""


class EventMetadata(BaseModel):
    summary: str
    is_digital: bool
    location_physical: Optional[str] = None
    registration_deadline: Optional[str] = None
    price_nok: Optional[float] = None
    organizer: Optional[str] = None
    instructor: Optional[str] = None
    language: Optional[str] = None
    audience: Optional[str] = None
    registration_url: Optional[str] = None


class EventEnrichment(BaseModel):
    index: int
    metadata: EventMetadata


class BatchResult(BaseModel):
    events: list[EventEnrichment]


def _call_batch(client: OpenAI, events_batch: list[dict]) -> list[EventEnrichment]:
    parts = []
    for i, ev in enumerate(events_batch):
        parts.append(
            f"=== Event {i} ===\n"
            f"Title: {ev['title']}\n"
            f"URL: {ev['url']}\n"
            f"Date: {ev['date_str']}\n"
            f"Listing location label: {ev['location'] or '(none)'}\n\n"
            f"Detail page text:\n{ev['detail_text']}"
        )
    user_content = (
        "\n\n".join(parts)
        + "\n\nExtract metadata for every event above. Return one entry per event, in input order."
    )

    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user_content},
        ],
        response_format={"type": "json_object"},
        max_tokens=4000,
        temperature=0,
    )
    raw = response.choices[0].message.content or "{}"
    data = json.loads(raw)
    return BatchResult.model_validate(data).events


def enrich_events(events_data: list[dict]) -> dict[str, dict]:
    """Call GitHub Models; return {uid: metadata_dict}. Empty on failure."""
    if not events_data:
        return {}

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        print("enrich: GITHUB_TOKEN not set — skipping", file=sys.stderr)
        return {}

    client = OpenAI(base_url=BASE_URL, api_key=token)

    out: dict[str, dict] = {}
    n_batches = 0
    for i in range(0, len(events_data), BATCH_SIZE):
        batch = events_data[i : i + BATCH_SIZE]
        n_batches += 1
        try:
            parsed = _call_batch(client, batch)
        except (OpenAIError, json.JSONDecodeError, ValueError) as e:
            print(f"enrich: batch {n_batches} failed, skipping: {e}", file=sys.stderr)
            continue
        for entry in parsed:
            if 0 <= entry.index < len(batch):
                uid = batch[entry.index]["uid"]
                out[uid] = entry.metadata.model_dump()

    print(
        f"enrich: {len(events_data)} new events in {n_batches} batch(es); "
        f"{len(out)} successfully enriched",
        file=sys.stderr,
    )
    return out
