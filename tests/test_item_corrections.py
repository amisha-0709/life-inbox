"""A saved source can be understood and corrected before organization."""
import sys

from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from tests.test_api import client


def capture(client, source="Call the team tomorrow"):
    response = client.post("/api/items", json={"raw_text": source})
    assert response.status_code == 200, response.text
    return response.json()


def test_pending_insight_read_and_correction_do_not_fabricate_analysis(client, monkeypatch):
    item = capture(client)
    main, db = sys.modules["app.main"], sys.modules["app.database"]
    monkeypatch.setattr(main, "propose_delta", lambda batch: (_ for _ in ()).throw(AssertionError("read called organizer")))
    insight = client.get(f"/api/items/{item['id']}/insight").json()
    assert insight["analysis_pending"] is True
    assert insight["analysis_run_id"] is None
    assert insight["rationale"] is None
    assert insight["has_user_overrides"] is False
    assert client.get(f"/api/items/{item['id']}/detail").status_code == 200
    with db.SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(db.AnalysisRunRecord)) == 0
        assert session.scalar(select(func.count()).select_from(db.ItemInsightRecord)) == 0
        assert session.scalar(select(func.count()).select_from(db.ItemAnalysisStateRecord)) == 0
    topic = client.post("/api/topics", json={"name": "My project"}).json()
    response = client.patch(f"/api/items/{item['id']}/insight", json={
        "user_title": "Team plan", "user_topic_id": topic["id"], "user_priority": "Low",
    })
    assert response.status_code == 200, response.text
    assert response.json()["analysis_pending"] is True
    saved = client.get(f"/api/items/{item['id']}").json()
    assert saved["effective_title"] == "Team plan"
    assert saved["effective_priority"] == "Low"
    assert saved["effective_topic_name"] == "My project"
    assert saved["raw_text"] == item["raw_text"]
    assert saved["time_reference_at"] == item["time_reference_at"]
    with db.SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(db.AnalysisRunRecord)) == 0
        assert session.scalar(select(func.count()).select_from(db.ItemAnalysisStateRecord)) == 0
        assert session.get(db.ItemInsightRecord, item["id"]).analysis_run_id is None


def test_pending_corrections_survive_first_analysis_explicit_reanalysis_and_new_connection(client):
    item = capture(client)
    topic = client.post("/api/topics", json={"name": "My household"}).json()
    assert client.patch(f"/api/items/{item['id']}/insight", json={
        "user_title": "Call home", "user_topic_id": topic["id"], "user_priority": "Low",
    }).status_code == 200
    result = client.post("/api/analyze")
    assert result.status_code == 200, result.text
    assert result.json()["metrics"]["target_ids"] == [item["id"]]
    assert client.get(f"/api/items/{item['id']}/insight").json()["analysis_pending"] is False
    before = client.get(f"/api/items/{item['id']}").json()
    assert client.post(f"/api/items/{item['id']}/invalidate-analysis").status_code == 200
    assert client.post("/api/analyze").status_code == 200
    db = sys.modules["app.database"]
    db.engine.dispose()
    with TestClient(client.app) as reopened:
        saved = reopened.get(f"/api/items/{item['id']}").json()
    for name in ("effective_title", "effective_priority", "effective_topic_id", "effective_topic_name"):
        assert saved[name] == before[name]
    assert saved["effective_title"] == "Call home"
    assert client.post("/api/analyze").json()["metrics"]["target_count"] == 0


def test_effective_filters_search_and_newest_pagination_match_corrections(client):
    first, second, third = [capture(client, source) for source in (
        "Call teacher tomorrow", "Buy rice today", "Read the saved article")]
    assert client.post("/api/analyze").status_code == 200
    topic = client.post("/api/topics", json={"name": "Family errands"}).json()
    assert client.patch(f"/api/items/{first['id']}/insight", json={
        "user_title": "My SPECIAL plan", "user_priority": "Low", "user_topic_id": topic["id"],
    }).status_code == 200
    for params in ({"q": "special"}, {"topic_id": topic["id"]}, {"category": "Family errands"}):
        rows = client.get("/api/items", params=params).json()
        assert [row["id"] for row in rows] == [first["id"]]
    rows = client.get("/api/items", params={"priority": "Low", "topic_id": topic["id"]}).json()
    assert [row["id"] for row in rows] == [first["id"]]
    assert client.get("/api/items", params={"priority": "High", "topic_id": topic["id"]}).json() == []
    assert client.get("/api/items", params={"sort": "newest", "limit": 1}).json()[0]["id"] == third["id"]
    assert client.get("/api/items", params={"sort": "newest", "limit": 1, "offset": 1}).json()[0]["id"] == second["id"]
    assert client.get("/api/items", params={"limit": 1}).json()[0]["id"] == first["id"]
    assert client.get("/api/items", params={"sort": "unknown"}).status_code == 422
    assert client.get("/api/items", params={"topic_id": -1}).status_code == 422
    assert client.patch(f"/api/items/{first['id']}/insight", json={"clear_overrides": True}).status_code == 200
    assert client.get("/api/items", params={"q": "special"}).json() == []
    cleared = client.get(f"/api/items/{first['id']}").json()
    assert cleared["effective_title"] != "My SPECIAL plan"


def test_preanalysis_corrections_and_detail_are_owner_scoped(client):
    assert client.post("/api/auth/dev-login", json={"email": "first-details@example.com"}).status_code == 200
    first = capture(client)
    duplicate = capture(client)
    assert client.post("/api/analyze").status_code == 200
    own_tasks = client.get("/api/tasks?status=all").json()
    with TestClient(client.app) as other:
        assert other.post("/api/auth/dev-login", json={"email": "second-details@example.com"}).status_code == 200
        foreign = capture(other, "PRIVATE second owner capture")
        foreign_topic = other.post("/api/topics", json={"name": "PRIVATE second topic"}).json()
        assert other.post("/api/analyze").status_code == 200
        assert other.get(f"/api/items/{first['id']}/detail").status_code == 404
        assert other.get(f"/api/items/{first['id']}/insight").status_code == 404
        assert other.patch(f"/api/items/{first['id']}/insight", json={"user_title": "overwrite"}).status_code == 404
        assert client.patch(f"/api/items/{first['id']}/insight", json={"user_topic_id": foreign_topic["id"]}).status_code == 400
        assert client.get("/api/items", params={"topic_id": foreign_topic["id"]}).json() == []
        db = sys.modules["app.database"]
        # Invalid historical relation rows must never appear in the detail.
        with db.SessionLocal() as session:
            a, b = sorted([first["id"], foreign["id"]])
            session.add(db.ItemRelationRecord(item_id_a=a, item_id_b=b, relation_type="related", score="0.8",
                                             dismissed=0, created_at=first["created_at"]))
            a, b = sorted([first["id"], duplicate["id"]])
            session.add(db.ItemRelationRecord(item_id_a=a, item_id_b=b, relation_type="related", score="0.8",
                                             dismissed=0, created_at=first["created_at"]))
            session.commit()
        detail = client.get(f"/api/items/{first['id']}/detail").json()
        assert detail["item"]["id"] == first["id"]
        assert {task["id"] for task in detail["tasks"]} == {task["id"] for task in own_tasks}
        assert {row["item"]["id"] for row in detail["related_items"]} == {duplicate["id"]}
        assert "PRIVATE" not in str(detail)
        assert client.get(f"/api/items/{foreign['id']}/detail").status_code == 404
        assert client.get("/api/items/99999/detail").status_code == 404


def test_legacy_foreign_topic_filter_uses_the_same_safe_fallback_as_details(client):
    item = capture(client)
    assert client.post("/api/analyze").status_code == 200
    db = sys.modules["app.database"]
    original = client.get(f"/api/items/{item['id']}").json()
    with TestClient(client.app) as other:
        other.post("/api/auth/dev-login", json={"email": "foreign-topic-filter@example.com"})
        foreign_topic = other.post("/api/topics", json={"name": "Private label"}).json()
        with db.SessionLocal() as session:
            session.get(db.ItemInsightRecord, item["id"]).user_topic_id = foreign_topic["id"]
            session.commit()
        saved = client.get(f"/api/items/{item['id']}").json()
        assert saved["effective_topic_id"] == original["effective_topic_id"]
        assert client.get("/api/items", params={"topic_id": foreign_topic["id"]}).json() == []
        rows = client.get("/api/items", params={"topic_id": original["effective_topic_id"]}).json()
        assert [row["id"] for row in rows] == [item["id"]]


def test_ask_sources_use_saved_title_corrections_without_organization(client, monkeypatch):
    import sys
    item = client.post('/api/items', json={'raw_text': 'The Cedar room is upstairs. Bring the blue notebook.'}).json()
    assert client.patch(f"/api/items/{item['id']}/insight", json={'user_title': 'Orchid workshop briefing'}).status_code == 200
    main = sys.modules['app.main']
    monkeypatch.setattr(main, 'propose_delta', lambda batch: (_ for _ in ()).throw(AssertionError('Read queued organization')))
    answer = client.post('/api/ask', json={'question': 'Where is the Orchid workshop briefing?'}).json()
    citation = next(row for row in answer['citations'] if row['item_id'] == item['id'])
    assert citation['title'] == 'Orchid workshop briefing'
    assert client.get('/api/analyze/latest').status_code == 204
