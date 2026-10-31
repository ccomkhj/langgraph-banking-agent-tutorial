"""Intent + slot extraction — the ONLY place an LLM is allowed to help (Q31).

The extractor turns user language into a small typed object (`Intent`).
It never calls tools, never writes SQL, never moves money.

Two implementations with the same interface:

* `MockExtractor`  — deterministic regex rules. Default. Used by all tests,
                     so nothing needs an API key.
* `LLMExtractor`   — any chat model via LangChain's `with_structured_output`.
                     Enable with  EXTRACTOR=llm  LLM_MODEL=anthropic:claude-haiku-5-5
                     (needs `uv sync --extra llm` and the provider's API key).

Whatever the extractor returns is then VALIDATED by deterministic Python in
`graph.validate` — LLM output is treated as a suggestion, not as truth.
"""

from __future__ import annotations

import os
import re
from typing import Literal

from pydantic import BaseModel, Field

IntentName = Literal["spending_summary", "transfer_money", "unknown"]
Period = Literal["this_month", "last_month"]


class Intent(BaseModel):
    """The structured output schema (see handoff §4 'Structured intent extraction')."""

    intent: IntentName = "unknown"
    # spending slots
    categories: list[str] = Field(default_factory=list, description="restaurants|groceries|transport")
    category_ambiguous: bool = Field(False, description="True for vague words like 'food'")
    period: Period | None = None
    # transfer slots
    recipient_query: str | None = None
    amount_minor: int | None = Field(None, description="amount in cents, 500 EUR -> 50000")
    currency: str | None = None


# ------------------------------------------------------------------ mock


CATEGORY_WORDS = {
    "restaurants": ["restaurant", "dining", "eating out", "eat out"],
    "groceries": ["grocer", "supermarket"],
    "transport": ["transport", "metro", "train", "bus", "taxi"],
}
AMBIGUOUS_WORDS = ["food", "eating", "meals"]  # could be restaurants OR groceries (Q32)
AMOUNT_RE = re.compile(r"(?:€|eur\s*)?(\d+(?:[.,]\d{1,2})?)\s*(?:€|eur|euros?)?", re.I)


def parse_categories(text: str) -> tuple[list[str], bool]:
    t = text.lower()
    if "both" in t:
        return ["restaurants", "groceries"], False
    cats = [c for c, words in CATEGORY_WORDS.items() if any(w in t for w in words)]
    ambiguous = not cats and any(w in t for w in AMBIGUOUS_WORDS)
    return cats, ambiguous


def parse_amount_minor(text: str) -> int | None:
    m = AMOUNT_RE.search(text)
    if not m:
        return None
    return round(float(m.group(1).replace(",", ".")) * 100)


class MockExtractor:
    """Keyword rules. Intentionally dumb and predictable — perfect for learning & tests."""

    name = "mock"

    def extract(self, text: str) -> Intent:
        t = text.lower()
        if re.search(r"\b(transfer|send|pay)\b", t):
            # "to Alex Novak" -> "Alex Novak" (capitalised words); "to sam" -> "Sam"
            m = re.search(r"\bto\s+([A-Z]\w*(?:\s+[A-Z]\w*)?)", text) or re.search(
                r"\bto\s+([a-z]\w*)", t
            )
            amount = parse_amount_minor(re.sub(r"\bto\s+.*$", "", t))
            return Intent(
                intent="transfer_money",
                recipient_query=m.group(1).title() if m else None,
                amount_minor=amount,
                currency="EUR" if amount is not None else None,
            )
        if re.search(r"\b(spend|spent|spending|cost)\b", t):
            cats, ambiguous = parse_categories(t)
            period = "last_month" if "last month" in t else "this_month" if "this month" in t else None
            return Intent(
                intent="spending_summary",
                categories=cats,
                category_ambiguous=ambiguous,
                period=period,
            )
        return Intent()


# ------------------------------------------------------------------ real LLM


class LLMExtractor:
    """Same interface, backed by a real chat model with structured output."""

    name = "llm"

    def __init__(self, model: str):
        from langchain.chat_models import init_chat_model  # optional dependency

        self._llm = init_chat_model(model, temperature=0).with_structured_output(Intent)

    def extract(self, text: str) -> Intent:
        system = (
            "Extract the banking intent from the user's message. "
            "Categories: restaurants, groceries, transport. If the user says a vague word "
            "like 'food', set category_ambiguous=true and leave categories empty. "
            "Never guess a recipient or amount that is not in the text. "
            "Amounts are in minor units (cents)."
        )
        return self._llm.invoke([("system", system), ("user", text)])


def make_extractor():
    if os.getenv("EXTRACTOR", "mock") == "llm":
        return LLMExtractor(os.environ.get("LLM_MODEL", "anthropic:claude-haiku-5-5"))
    return MockExtractor()
