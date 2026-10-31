import importlib

from fastapi.testclient import TestClient

from app.evaluation import run_eval


def test_mock_extractor_eval_is_perfect_and_never_false_confident():
    r = run_eval()
    assert r["intent_accuracy"] == r["slot_accuracy"] == r["route_accuracy"] == 1.0
    assert r["false_confident"] == 0


def test_http_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import app.main
    main = importlib.reload(app.main)
    c = TestClient(main.app)
    assert c.get("/").status_code == 200
    assert "extract_intent" in c.get("/api/graph").json()["nodes"]
    assert len(c.get("/api/notes").json()) == 33
    r = c.post("/api/chat", json={"text": "Send 20 EUR to Sam"}).json()
    tx = r["interrupt"]["transfer_id"]
    assert c.post(f"/api/bank/transfers/{tx}/approve").json()["thread_id"] == r["thread_id"]
    assert c.post(f"/api/bank/transfers/{tx}/approve").status_code == 409  # not pending anymore
    assert c.post("/api/bank/worker/run").json() == {"processed": [tx]}
    assert c.post(f"/api/bank/transfers/{tx}/replay").json()["payment_did_work"] is False
