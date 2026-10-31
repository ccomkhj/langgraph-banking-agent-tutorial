from datetime import date

import pytest

from app import bank
from app.graph import Assistant

TODAY = date(2026, 10, 15)  # fixed date => deterministic "last month" totals


@pytest.fixture
def paths(tmp_path):
    return str(tmp_path / "bank.db"), str(tmp_path / "agent.db")


@pytest.fixture
def conn(paths):
    c = bank.connect(paths[0])
    bank.init_db(c, today=TODAY)
    return c


@pytest.fixture
def assistant(conn, paths):
    return Assistant(conn, paths[1], today=TODAY)


def prepare(assistant, text):
    """Chat until the graph pauses for approval; return (thread_id, transfer_id)."""
    r = assistant.chat(text)
    assert r["interrupt"]["kind"] == "approval", r["interrupt"]
    return r["thread_id"], r["interrupt"]["transfer_id"]
