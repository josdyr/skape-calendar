"""Call Claude to extract structured metadata for a batch of scraped events."""
from __future__ import annotations

import os
import sys
from typing import Optional

import anthropic
from pydantic import BaseModel, Field

MODEL = "claude-haiku-4-5"

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
- "Gratis"/"Free"/"GRATIS" → price_nok=0. If a number like "300,-" appears, extract it as price_nok=300.
- registration_deadline: return ISO 8601 date (YYYY-MM-DD). If only day+month is given, assume the same year as the event date. Return null if absent.
- summary: one or two sentences in English describing what attendees will learn or do. Neutral, concrete.
- Preserve Norwegian text verbatim in location, organizer, instructor — do not translate these.
- language: use English language name ("Norwegian", "English", "Norwegian/English").
- Never fabricate data. Use null for anything not clearly stated on the page.
- Return exactly one entry per event, keyed by the integer index you were given. Maintain input order.
"""


class EventMetadata(BaseModel):
    summary: str = Field(description="1-2 sentence English description of the event")
    is_digital: bool = Field(description="True if event is fully online (webinar), false if physical/in-person")
    location_physical: Optional[str] = Field(description="Physical venue/address for in-person events; null if digital or unknown")
    registration_deadline: Optional[str] = Field(description="ISO 8601 date (YYYY-MM-DD) of registration deadline; null if absent")
    price_nok: Optional[float] = Field(description="Price in NOK; 0 for free events; null if unknown")
    organizer: Optional[str] = Field(description="Event organizer (e.g. 'Skape'); null if unknown")
    instructor: Optional[str] = Field(description="Instructor/speaker name and affiliation; null if unknown")
    language: Optional[str] = Field(description="Event language in English (e.g. 'Norwegian', 'English')")
    audience: Optional[str] = Field(description="Target audience description; null if unspecified")
    registration_url: Optional[str] = Field(description="URL to registration form; null if not present on page")


class EventEnrichment(BaseModel):
    index: int = Field(description="0-based index of the event in the input list")
    metadata: EventMetadata


class BatchResult(BaseModel):
    events: list[EventEnrichment]


def enrich_events(events_data: list[dict]) -> dict[str, dict]:
    """Call Claude for the given events; return {uid: metadata_dict}. Empty on failure."""
    if not events_data:
        return {}

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("enrich: ANTHROPIC_API_KEY not set — skipping", file=sys.stderr)
        return {}

    client = anthropic.Anthropic()

    parts = []
    for i, ev in enumerate(events_data):
        parts.append(
            f"=== Event {i} ===\n"
            f"Title: {ev['title']}\n"
            f"URL: {ev['url']}\n"
            f"Date: {ev['date_str']}\n"
            f"Listing location label: {ev['location'] or '(none)'}\n\n"
            f"Detail page text:\n{ev['detail_text']}"
        )
    user_content = "\n\n".join(parts) + "\n\nExtract metadata for every event. Return one entry per event, in the same order."

    try:
        response = client.messages.parse(
            model=MODEL,
            max_tokens=16000,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_content}],
            output_format=BatchResult,
        )
    except anthropic.APIError as e:
        print(f"enrich: API error — skipping enrichment: {e}", file=sys.stderr)
        return {}

    u = response.usage
    print(
        f"enrich: {len(events_data)} new events, "
        f"input={u.input_tokens} output={u.output_tokens} "
        f"cache_read={u.cache_read_input_tokens} cache_create={u.cache_creation_input_tokens}",
        file=sys.stderr,
    )

    out: dict[str, dict] = {}
    for entry in response.parsed_output.events:
        if 0 <= entry.index < len(events_data):
            uid = events_data[entry.index]["uid"]
            out[uid] = entry.metadata.model_dump()
    return out
