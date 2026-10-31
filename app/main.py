"""FastAPI: thin HTTP layer over the Assistant (graph) and the mock bank.

Run:  uv run uvicorn app.main:app --reload     then open http://127.0.0.1:8000

Two groups of routes — keep them apart in your head:
  /api/chat, /api/threads/...   -> the AGENT (LangGraph)
  /api/bank/...                 -> the MOCK BANK (source of truth, trusted approval)
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app import bank
from app.evaluation import run_eval
from app.extractor import make_extractor
from app.graph import NODE_INFO, Assistant

ROOT = Path(__file__).parent.parent
DATA = Path(os.getenv("DATA_DIR", ROOT / "data"))
DATA.mkdir(exist_ok=True)

bank_conn = bank.connect(str(DATA / "bank.db"))
bank.init_db(bank_conn)
assistant = Assistant(bank_conn, str(DATA / "agent.db"), extractor=make_extractor())

app = FastAPI(title="LangGraph banking learning app (fictional data)")


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


# ------------------------------------------------------------------ agent


class ChatIn(BaseModel):
    text: str
    thread_id: str | None = None  # set when answering a clarification question


@app.post("/api/chat")
def chat(body: ChatIn):
    return assistant.chat(body.text, body.thread_id)


@app.get("/api/threads/{thread_id}")
def thread(thread_id: str):
    return {"thread_id": thread_id, **assistant.snapshot(thread_id)}


@app.get("/api/graph")
def graph():
    return {"mermaid": assistant.graph.get_graph().draw_mermaid(), "nodes": NODE_INFO,
            "extractor": assistant.extractor.name}


# ------------------------------------------------------------------ mock bank


@app.get("/api/bank")
def bank_overview():
    transfers = [{**t, "thread_id": assistant.thread_for(t["id"])} for t in bank.list_transfers(bank_conn)]
    return {
        "balance": bank.balance(bank_conn),
        "transfers": transfers,
        "notifications": bank.list_notifications(bank_conn),
        "transactions": [dict(r) for r in bank_conn.execute("SELECT * FROM transactions ORDER BY day")],
    }


def _decide(transfer_id: str, approve: bool):
    try:
        return assistant.decide(transfer_id, approve)
    except (ValueError, KeyError) as e:
        raise HTTPException(409, str(e))


# These two simulate the bank app's own approval screen. In a real bank this is
# where a device-bound signature over (id, recipient, amount) is verified (Q15-Q17).
@app.post("/api/bank/transfers/{transfer_id}/approve")
def approve(transfer_id: str):
    return _decide(transfer_id, True)


@app.post("/api/bank/transfers/{transfer_id}/reject")
def reject(transfer_id: str):
    return _decide(transfer_id, False)


@app.post("/api/bank/worker/run")
def run_worker():
    return {"processed": bank.run_worker(bank_conn)}


@app.post("/api/bank/transfers/{transfer_id}/replay")
def replay(transfer_id: str):
    """Simulate a DUPLICATE event: run the payment + notification again."""
    return {"payment_did_work": bank.execute_payment(bank_conn, transfer_id),
            "new_notification": bank.notify(bank_conn, transfer_id, "TransferCompleted"),
            "balance": bank.balance(bank_conn)}


# ------------------------------------------------------------------ learning


@app.get("/api/notes")
def notes():
    """Parse '### Q01 — title' sections out of docs/mock_case.md."""
    text = (ROOT / "docs" / "mock_case.md").read_text()
    out = []
    for m in re.finditer(r"^### (Q\d+) — (.+?)\n(.*?)(?=^### |^---|\Z)", text, re.S | re.M):
        out.append({"id": m.group(1), "title": m.group(2), "body": m.group(3).strip()})
    return out


@app.get("/api/eval")
def evaluation():
    return run_eval(assistant.extractor)
