from fastapi.testclient import TestClient
from app.main import app


def test_api_flow():
    with TestClient(app) as c:
        assert c.get("/health").json() == {"status": "ok"}
        r = c.post("/api/runs", json={"objective": "Find dental clinics", "input": {"industry": "dental"}})
        assert r.status_code == 202
        run_id = r.json()["run_id"]
        # TestClient finishes background tasks before returning: the run is paused at the email
        d = c.get(f"/api/runs/{run_id}").json()
        assert d["status"] == "awaiting_approval" and len(d["tasks"]) == 8
        pending = c.get("/api/approvals", params={"status": "pending", "run_id": run_id}).json()
        assert len(pending) == 1 and pending[0]["action_type"] == "send_email"
        r = c.post(f"/api/approvals/{pending[0]['id']}/decision", json={"decision": "approve"})
        assert r.status_code == 200 and r.json()["status"] == "approved"
        d = c.get(f"/api/runs/{run_id}").json()
        assert d["status"] == "succeeded"
        assert all(t["status"] == "succeeded" for t in d["tasks"])
        assert d["report_md"]
        assert d["plan_json"]["tasks"] and len(d["plan_json"]["tasks"]) == 8   # plan is exposed to the dashboard
        ev = c.get(f"/api/runs/{run_id}/events").json()
        assert [e["type"] for e in ev][:3] == ["run_created", "run_started", "plan_created"]
        assert ev[0]["type"] == "run_created" and ev[-1]["type"] == "run_succeeded"
        later = c.get(f"/api/runs/{run_id}/events", params={"after_id": ev[2]["id"]}).json()
        assert len(later) == len(ev) - 3
        assert any(x["id"] == run_id for x in c.get("/api/runs").json())
        assert c.get("/api/runs/nope").status_code == 404
        assert c.post("/api/runs", json={"objective": "x"}).status_code == 422
