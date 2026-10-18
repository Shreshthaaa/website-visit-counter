import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    # Entering the context runs the lifespan handler.
    with TestClient(app) as test_client:
        yield test_client


def test_liveness_does_not_depend_on_redis(client):
    assert client.get("/healthz").status_code == 200


def test_readiness_reports_shards(client):
    response = client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert len(body["shards"]) == 2


def test_version_exposes_build_identity(client):
    body = client.get("/version").json()
    assert body == {"version": "test", "git_sha": "testsha"}


def test_root(client):
    assert client.get("/").json() == {"status": "healthy"}


def test_record_then_read_visit(client):
    recorded = client.post("/api/v1/counter/visit/homepage")
    assert recorded.status_code == 200
    assert recorded.json()["status"] == "success"

    body = client.get("/api/v1/counter/visits/homepage").json()
    assert body["count"] == 1
    assert "served_via" in body


def test_response_carries_request_id(client):
    response = client.post("/api/v1/counter/visit/page-x")
    assert response.headers.get("x-request-id")


def test_supplied_request_id_is_echoed(client):
    response = client.get("/healthz", headers={"x-request-id": "abc123"})
    assert response.headers["x-request-id"] == "abc123"


def test_metrics_exposes_application_series(client):
    client.post("/api/v1/counter/visit/metric-page")
    body = client.get("/metrics").text

    assert "visit_counter_visits_recorded_total" in body
    assert "visit_counter_buffer_depth" in body
    assert 'visit_counter_build_info{git_sha="testsha",version="test"}' in body


def test_http_metrics_use_the_route_template(client):
    client.post("/api/v1/counter/visit/page-one")
    client.post("/api/v1/counter/visit/page-two")
    body = client.get("/metrics").text

    # Both requests must collapse onto one series. Labelling by the raw path
    # would let a caller create unbounded cardinality.
    assert 'route="/api/v1/counter/visit/{page_id}"' in body
    assert "page-one" not in body
    assert "page-two" not in body
    assert "visit_counter_http_request_duration_seconds_bucket" in body


def test_unmatched_paths_do_not_create_series(client):
    client.get("/no/such/route/xyz123")
    body = client.get("/metrics").text

    assert 'route="unmatched"' in body
    assert "xyz123" not in body
