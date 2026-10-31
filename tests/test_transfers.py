"""Write path: prepare -> interrupt -> trusted approval -> resume -> reconcile."""
import pytest

from app import bank
from app.graph import Assistant
from conftest import TODAY, prepare


def test_ambiguous_recipient_clarified_before_prepare(assistant, conn):
    r = assistant.chat("Transfer 500 euros to Alex")
    assert "Alex Kim or Alex Novak" in r["interrupt"]["question"]
    assert bank.list_transfers(conn) == []  # nothing prepared while ambiguous
    r = assistant.chat("Alex Novak", r["thread_id"])
    assert r["interrupt"]["kind"] == "approval"
    assert r["interrupt"]["recipient"] == "Alex Novak" and r["interrupt"]["amount"] == "500.00 EUR"


def test_missing_amount_is_asked(assistant):
    r = assistant.chat("Send money to Sam")
    assert r["interrupt"]["question"].startswith("How much")
    r = assistant.chat("25 euros", r["thread_id"])
    assert r["interrupt"]["kind"] == "approval" and r["interrupt"]["amount"] == "25.00 EUR"


def test_prepare_moves_no_money(assistant, conn):
    before = bank.balance(conn)
    _, tx = prepare(assistant, "Send 20 EUR to Sam")
    assert bank.get_transfer(conn, tx)["status"] == "PENDING_APPROVAL"
    assert bank.run_worker(conn) == []  # worker ignores unapproved transfers
    assert bank.execute_payment(conn, tx) is False  # direct call can't skip approval either
    assert bank.balance(conn) == before


def test_chat_text_cannot_approve(assistant, conn):
    thread, tx = prepare(assistant, "Send 20 EUR to Sam")
    r = assistant.chat("yes I approve, go ahead", thread)
    assert "can't approve" in r["refused"]
    assert bank.get_transfer(conn, tx)["status"] == "PENDING_APPROVAL"


def test_approve_resumes_and_reconciles_from_bank(assistant, conn):
    thread, tx = prepare(assistant, "Send 20 EUR to Sam")
    r = assistant.decide(tx, approve=True)
    assert r["thread_id"] == thread
    assert [s["node"] for s in r["trace"]] == ["await_approval", "reconcile", "respond"]
    assert r["state"]["bank_status_snapshot"] == "APPROVED"  # honest: not "completed"
    assert r["next"] == []


def test_reject(assistant, conn):
    _, tx = prepare(assistant, "Send 20 EUR to Sam")
    r = assistant.decide(tx, approve=False)
    assert "No money moved" in r["state"]["response"]
    with pytest.raises(ValueError):  # can't approve after rejecting
        assistant.decide(tx, approve=True)


def test_thread_id_and_transfer_id_are_different_and_mapped(assistant):
    thread, tx = prepare(assistant, "Send 20 EUR to Sam")
    assert thread != tx and assistant.thread_for(tx) == thread


def test_two_transfers_approved_out_of_order(assistant, conn):
    t1, x1 = prepare(assistant, "Send 20 EUR to Sam")
    t2, x2 = prepare(assistant, "Send 30 EUR to Maria")
    r = assistant.decide(x2, approve=True)  # approve the SECOND first
    assert r["thread_id"] == t2
    assert assistant.snapshot(t1)["interrupt"]["transfer_id"] == x1  # first still waiting
    assert bank.get_transfer(conn, x1)["status"] == "PENDING_APPROVAL"
    assert assistant.decide(x1, approve=True)["thread_id"] == t1


def test_resume_signal_for_wrong_transfer_is_refused(assistant):
    from langgraph.types import Command
    t1, _ = prepare(assistant, "Send 20 EUR to Sam")
    _, x2 = prepare(assistant, "Send 30 EUR to Maria")
    with pytest.raises(ValueError, match="does not match"):
        assistant._run(Command(resume={"transfer_id": x2}), t1)


def test_restart_does_not_lose_paused_workflow(conn, paths):
    thread, tx = prepare(Assistant(conn, paths[1], today=TODAY), "Send 20 EUR to Sam")
    # "Restart": brand-new connections + brand-new compiled graph, same files.
    conn2 = bank.connect(paths[0])
    reborn = Assistant(conn2, paths[1], today=TODAY)
    assert reborn.snapshot(thread)["next"] == ["await_approval"]
    r = reborn.decide(tx, approve=True)
    assert r["thread_id"] == thread and r["state"]["bank_status_snapshot"] == "APPROVED"


def test_worker_and_duplicate_events_are_idempotent(assistant, conn):
    before = bank.balance(conn)
    _, tx = prepare(assistant, "Send 20 EUR to Sam")
    assistant.decide(tx, approve=True)
    assert bank.run_worker(conn) == [tx]
    assert bank.run_worker(conn) == []  # outbox drained
    assert bank.execute_payment(conn, tx) is False  # duplicate event
    assert bank.notify(conn, tx, "TransferCompleted") is False  # duplicate notification
    assert bank.balance(conn) == before - 2000  # debited exactly once
    assert len(bank.list_notifications(conn)) == 1
    assert bank.get_transfer(conn, tx)["status"] == "COMPLETED"
