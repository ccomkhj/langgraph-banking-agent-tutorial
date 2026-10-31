"""Tiny offline evaluation for intent + slot extraction + routing (handoff §5).

Each labeled case says what the extractor SHOULD produce and what the graph
SHOULD do next:  "answer" (finished), "clarify" (asked a question) or
"approval" (prepared a transfer and paused for the bank).

Run:  uv run python -m app.evaluation          (mock extractor)
      EXTRACTOR=llm uv run python -m app.evaluation   (your real LLM)
"""

from __future__ import annotations

import json
import tempfile
from collections import Counter
from pathlib import Path

from app import bank
from app.extractor import make_extractor
from app.graph import Assistant

CASES = Path(__file__).parent.parent / "evaluation" / "labeled_cases.jsonl"
SLOT_KEYS = ("categories", "period", "recipient_query", "amount_minor")


def run_eval(extractor=None) -> dict:
    extractor = extractor or make_extractor()
    tmp = tempfile.mkdtemp()  # throwaway bank, so eval never touches your demo data
    conn = bank.connect(f"{tmp}/bank.db")
    bank.init_db(conn)
    assistant = Assistant(conn, f"{tmp}/agent.db", extractor=extractor)

    rows, score = [], Counter()
    for case in map(json.loads, CASES.read_text().splitlines()):
        got = extractor.extract(case["text"])
        run = assistant.chat(case["text"])
        outcome = run["interrupt"]["kind"] if run["interrupt"] else "answer"
        checks = {
            "intent": got.intent == case["intent"],
            "slots": all(getattr(got, k) == case[k] for k in SLOT_KEYS if k in case),
            "outcome": outcome == case["outcome"],
        }
        # The dangerous error: proceeding when we should have asked (Q33).
        false_confident = case["outcome"] == "clarify" and outcome != "clarify"
        score.update({k for k, ok in checks.items() if ok})
        score["false_confident"] += false_confident
        rows.append({"text": case["text"], "expected": case["outcome"], "got": outcome,
                     "predicted_intent": got.intent, **checks, "false_confident": false_confident})
    n = len(rows)
    return {
        "extractor": extractor.name,
        "n": n,
        "intent_accuracy": score["intent"] / n,
        "slot_accuracy": score["slots"] / n,
        "route_accuracy": score["outcome"] / n,
        "false_confident": score["false_confident"],
        "rows": rows,
    }


if __name__ == "__main__":
    r = run_eval()
    for row in r["rows"]:
        ok = "✓" if row["intent"] and row["slots"] and row["outcome"] else "✗"
        print(f"{ok} {row['expected']:>8} -> {row['got']:<8} {row['text']}")
    print({k: v for k, v in r.items() if k != "rows"})
