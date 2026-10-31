"""The LangGraph banking assistant: ONE StateGraph (Q22).

Read this file top to bottom — it is the whole agent.

    START
      │
      ▼
    extract_intent      (LLM or mock: language -> typed Intent)
      │
      ▼
    validate  ◄──────── clarify   (interrupt(): ask the user, wait, merge answer)
      │  conditional edge `route_after_validate`
      ├── "clarify"  ──► clarify
      ├── "spending" ──► read_spending ───────────────────────────────┐
      ├── "transfer" ──► prepare_transfer ─► await_approval ─► reconcile ┤
      └── "unknown"  ────────────────────────────────────────────────┤
                                                                      ▼
                                                                   respond ─► END

Two kinds of pause, both with `interrupt()` (Q26):
  * clarify         — resumed by the user's next chat message
  * await_approval  — resumed ONLY by the trusted bank approval endpoint
"""

from __future__ import annotations

import json
import operator
import sqlite3
import uuid
from datetime import date
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app import bank
from app.extractor import MockExtractor, parse_amount_minor, parse_categories

# =========================================================================
# 1. STATE — per-thread shared data (Q23, Q24, Q25)
# =========================================================================


class State(TypedDict, total=False):
    # `Annotated[..., operator.add]` is a REDUCER: node updates are APPENDED.
    # Every other key is simply REPLACED by the latest node that returns it.
    messages: Annotated[list[dict], operator.add]
    intent: str  # "spending_summary" | "transfer_money" | "unknown"
    slots: dict  # validated slot values (categories, period, amount...)
    question: dict | None  # {"field": ..., "text": ...} when we must ask
    route: str  # decided by `validate`, read by the conditional edge
    recipient: dict | None  # resolved contact {"id", "name"}
    result: dict | None  # tool output (read path)
    transfer_id: str | None  # the BANK's id — not the thread id! (Q28)
    workflow_status: str  # the GRAPH's view: where are we in the workflow?
    bank_status_snapshot: str | None  # last status READ from the bank (never authoritative)
    response: str | None  # final text for the user


# What each node is, for the UI + docs. "topics" link to mock-case design questions.
NODE_INFO = {
    "extract_intent": {"kind": "LLM", "topics": ["Q24", "Q31", "Q32"],
                       "explain": "Turns the user's words into a typed Intent. The only LLM step."},
    "validate": {"kind": "Python", "topics": ["Q13", "Q33"],
                 "explain": "Deterministic policy: checks slots, resolves the recipient in the bank, decides the route."},
    "clarify": {"kind": "interrupt", "topics": ["Q32", "Q33", "Q26"],
                "explain": "Pauses the graph with interrupt() and waits for the user's answer."},
    "read_spending": {"kind": "Python + bank API", "topics": ["Q31", "Q12"],
                      "explain": "Fixed read-only bank query + deterministic sum. Tool output is DATA, never instructions."},
    "prepare_transfer": {"kind": "Python + bank API", "topics": ["Q04", "Q25"],
                         "explain": "Bank creates a PENDING_APPROVAL transfer id. No money moves."},
    "await_approval": {"kind": "interrupt", "topics": ["Q26", "Q27", "Q28", "Q29", "Q30"],
                       "explain": "Pauses until the bank (not the LLM, not chat text) records an approval."},
    "reconcile": {"kind": "Python + bank API", "topics": ["Q07", "Q08", "Q09"],
                  "explain": "Re-reads the real status from the bank. The bank is the source of truth."},
    "respond": {"kind": "Python template", "topics": ["Q31", "Q09"],
                "explain": "Templated answer from validated data, so numbers can't be hallucinated."},
}


def eur(minor: int) -> str:
    return f"{minor / 100:,.2f} EUR"


def last_user_text(state: State) -> str:
    return next(m["content"] for m in reversed(state["messages"]) if m["role"] == "user")


# =========================================================================
# 2. NODES + 3. EDGES, built inside a function so dependencies are explicit
# =========================================================================


def build_graph(bank_conn, extractor, checkpointer, today: date | None = None,
                on_transfer_prepared=None):
    today_fn = (lambda: today) if today else date.today

    # ---------------------------------------------------------------- nodes
    # A node = a plain function: (state) -> partial state update (a dict).

    def extract_intent(state: State) -> dict:
        intent = extractor.extract(last_user_text(state))
        return {"intent": intent.intent, "slots": intent.model_dump(exclude={"intent"})}

    def validate(state: State) -> dict:
        """Deterministic policy. Decides: clarify, spending, transfer or unknown."""
        slots = dict(state["slots"])
        ask = lambda field, text: {"route": "clarify", "question": {"field": field, "text": text}, "slots": slots}

        if state["intent"] == "spending_summary":
            if slots.get("category_ambiguous") or not slots.get("categories"):
                return ask("category", "Do you mean restaurants, groceries, or both?")
            bad = [c for c in slots["categories"] if c not in bank.CATEGORIES]
            if bad:  # never pass an LLM-invented category to the bank
                return ask("category", f"I don't know the category {bad}. Restaurants, groceries or transport?")
            if not slots.get("period"):
                slots["period"] = "this_month"
                slots["period_assumed"] = True  # read-only + low risk: proceed, but say so
            return {"route": "spending", "slots": slots, "question": None}

        if state["intent"] == "transfer_money":
            if not slots.get("recipient_query"):
                return ask("recipient", "Who should receive the money?")
            matches = bank.find_contacts(bank_conn, slots["recipient_query"])
            if not matches:
                return ask("recipient", f"I can't find '{slots['recipient_query']}' in your contacts. Who do you mean?")
            if len(matches) > 1:  # two Alexes -> NEVER guess on a money movement (Q04)
                names = " or ".join(m["name"] for m in matches)
                return ask("recipient", f"Which one do you mean: {names}?")
            if not slots.get("amount_minor"):
                return ask("amount", f"How much should I send to {matches[0]['name']}?")
            return {"route": "transfer", "recipient": matches[0], "slots": slots, "question": None}

        return {"route": "unknown", "question": None}

    def clarify(state: State) -> dict:
        q = state["question"]
        # --- PAUSE HERE. The graph is checkpointed and the HTTP request returns.
        # When resumed with Command(resume="restaurants"), interrupt() returns "restaurants".
        # NOTE: on resume this node re-runs from the top, so keep code above
        # interrupt() free of side effects (Q26 nuance).
        answer: str = interrupt({"kind": "clarify", "question": q["text"]})

        slots = dict(state["slots"])
        if q["field"] == "category":
            slots["categories"], slots["category_ambiguous"] = parse_categories(answer)
        elif q["field"] == "recipient":
            slots["recipient_query"] = answer.strip().rstrip(".?!")
        elif q["field"] == "amount":
            slots["amount_minor"] = parse_amount_minor(answer)
            slots["currency"] = "EUR"
        return {"messages": [{"role": "user", "content": answer}], "slots": slots, "question": None}

    def read_spending(state: State) -> dict:
        s = state["slots"]
        start, end = bank.month_bounds(today_fn(), 1 if s["period"] == "last_month" else 0)
        txs = bank.list_transactions(bank_conn, s["categories"], start, end)
        # Deterministic arithmetic. Merchant names are untrusted DATA: we only
        # display them; nothing in this path can reach a write tool.
        return {"result": {
            "total_minor": sum(t["amount_minor"] for t in txs),
            "count": len(txs),
            "label": start.strftime("%B %Y"),
            "transactions": txs,
        }}

    def prepare_transfer(state: State, config) -> dict:
        # Separate node BEFORE the interrupt so it does not re-run on resume (Q26).
        s = state["slots"]
        t = bank.prepare_transfer(bank_conn, state["recipient"]["id"], s["amount_minor"], s.get("currency") or "EUR")
        if on_transfer_prepared:  # durable transfer_id -> thread_id mapping (Q29)
            on_transfer_prepared(t["id"], config["configurable"]["thread_id"])
        return {"transfer_id": t["id"], "bank_status_snapshot": t["status"],
                "workflow_status": "WAITING_FOR_APPROVAL"}

    def await_approval(state: State) -> dict:
        signal = interrupt({
            "kind": "approval",
            "transfer_id": state["transfer_id"],
            "recipient": state["recipient"]["name"],
            "amount": eur(state["slots"]["amount_minor"]),
        })
        # Defence in depth: the resume signal must be about OUR transfer.
        if signal.get("transfer_id") != state["transfer_id"]:
            raise ValueError("resume signal does not match this thread's transfer")
        return {"workflow_status": "RESUMED_AFTER_BANK_DECISION"}

    def reconcile(state: State) -> dict:
        t = bank.get_transfer(bank_conn, state["transfer_id"])  # ask the source of truth
        return {"bank_status_snapshot": t["status"], "workflow_status": "DONE"}

    def respond(state: State) -> dict:
        route = state.get("route")
        if route == "spending":
            r, s = state["result"], state["slots"]
            text = (f"You spent {eur(r['total_minor'])} on {' + '.join(s['categories'])} "
                    f"in {r['label']} ({r['count']} transactions).")
            if s.get("period_assumed"):
                text += " (You didn't say when, so I assumed this month.)"
        elif route == "transfer":
            status = state["bank_status_snapshot"]
            who, amt = state["recipient"]["name"], eur(state["slots"]["amount_minor"])
            text = {
                "APPROVED": f"The bank recorded your approval of {amt} to {who}. Status is APPROVED "
                            f"(queued, not completed yet) — you'll get a notification when it completes.",
                "COMPLETED": f"{amt} to {who} is COMPLETED.",
                "REJECTED": f"You rejected the transfer of {amt} to {who}. No money moved.",
            }.get(status, f"The bank reports status {status} for this transfer.")
        else:
            text = ("I can summarise spending (e.g. 'How much did I spend on restaurants last month?') "
                    "or prepare a transfer (e.g. 'Transfer 50 euros to Sam').")
        return {"response": text, "messages": [{"role": "assistant", "content": text}]}

    # ---------------------------------------------------------------- edges

    def route_after_validate(state: State) -> str:
        # A conditional edge is just a function returning the NAME of the next node.
        return {"clarify": "clarify", "spending": "read_spending",
                "transfer": "prepare_transfer"}.get(state["route"], "respond")

    g = StateGraph(State)
    for name, fn in [("extract_intent", extract_intent), ("validate", validate), ("clarify", clarify),
                     ("read_spending", read_spending), ("prepare_transfer", prepare_transfer),
                     ("await_approval", await_approval), ("reconcile", reconcile), ("respond", respond)]:
        g.add_node(name, fn)

    g.add_edge(START, "extract_intent")
    g.add_edge("extract_intent", "validate")
    g.add_conditional_edges("validate", route_after_validate,
                            ["clarify", "read_spending", "prepare_transfer", "respond"])
    g.add_edge("clarify", "validate")  # loop back after the user answers
    g.add_edge("read_spending", "respond")
    g.add_edge("prepare_transfer", "await_approval")
    g.add_edge("await_approval", "reconcile")
    g.add_edge("reconcile", "respond")
    g.add_edge("respond", END)

    # Compiling with a checkpointer = every step is saved under its thread_id.
    return g.compile(checkpointer=checkpointer)


# =========================================================================
# 4. ASSISTANT — glue between HTTP, the graph, and the bank
# =========================================================================


class Assistant:
    """Owns the compiled graph, the checkpointer, and the transfer->thread map.

    Two SQLite files, on purpose (Q07, Q27):
      bank.db   — the mock bank (money, authoritative)
      agent.db  — LangGraph checkpoints + transfer_threads map (workflow, not money)
    """

    def __init__(self, bank_conn, agent_db_path: str, extractor=None, today: date | None = None):
        self.bank = bank_conn
        self.agent_conn = sqlite3.connect(agent_db_path, check_same_thread=False)
        self.agent_conn.execute(
            "CREATE TABLE IF NOT EXISTS transfer_threads(transfer_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL)"
        )
        self.agent_conn.commit()
        checkpointer = SqliteSaver(self.agent_conn)  # survives restarts (Q27)
        self.extractor = extractor or MockExtractor()
        self.graph = build_graph(bank_conn, self.extractor, checkpointer, today, self._remember)

    # -- transfer_id -> thread_id ------------------------------------------
    def _remember(self, transfer_id: str, thread_id: str) -> None:
        self.agent_conn.execute("INSERT OR REPLACE INTO transfer_threads VALUES (?, ?)", (transfer_id, thread_id))
        self.agent_conn.commit()

    def thread_for(self, transfer_id: str) -> str | None:
        row = self.agent_conn.execute(
            "SELECT thread_id FROM transfer_threads WHERE transfer_id = ?", (transfer_id,)).fetchone()
        return row[0] if row else None

    # -- running the graph ---------------------------------------------------
    def _run(self, graph_input: Any, thread_id: str) -> dict:
        config = {"configurable": {"thread_id": thread_id}}
        trace = []
        # stream_mode="updates" yields {node_name: what_that_node_returned} per step.
        for step in self.graph.stream(graph_input, config, stream_mode="updates"):
            for node, update in step.items():
                if node == "__interrupt__":
                    trace.append({"node": "⏸ interrupt", "update": [i.value for i in update]})
                else:
                    trace.append({"node": node, "update": update})
        return {"thread_id": thread_id, "trace": json.loads(json.dumps(trace, default=str)),
                **self.snapshot(thread_id)}

    def snapshot(self, thread_id: str) -> dict:
        s = self.graph.get_state({"configurable": {"thread_id": thread_id}})
        return {
            "state": json.loads(json.dumps(s.values, default=str)),
            "next": list(s.next),  # nodes that will run on resume ([] = finished)
            "interrupt": s.interrupts[0].value if s.interrupts else None,
        }

    def chat(self, text: str, thread_id: str | None = None) -> dict:
        if thread_id:
            pending = self.snapshot(thread_id)["interrupt"]
            if pending and pending["kind"] == "clarify":
                return self._run(Command(resume=text), thread_id)  # answer the question
            if pending and pending["kind"] == "approval":
                # "yes, approve it" typed in chat is NOT a bank approval (Q03, Q12).
                return {"thread_id": thread_id, "trace": [], **self.snapshot(thread_id),
                        "refused": "This thread is waiting for approval in the bank. "
                                   "Chat text can't approve a transfer — use the bank approval button."}
        # Otherwise: a brand-new request gets a brand-new thread (Q29 simple rule).
        new_thread = "thread-" + uuid.uuid4().hex[:8]
        return self._run({"messages": [{"role": "user", "content": text}]}, new_thread)

    def decide(self, transfer_id: str, approve: bool) -> dict:
        """Trusted approval path: 1) bank records the decision, 2) resume the RIGHT thread."""
        bank.decide_transfer(self.bank, transfer_id, approve)  # raises if not PENDING_APPROVAL
        thread_id = self.thread_for(transfer_id)
        if not thread_id:
            raise KeyError(f"no workflow thread for {transfer_id}")
        return self._run(Command(resume={"transfer_id": transfer_id}), thread_id)
