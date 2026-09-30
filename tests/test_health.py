"""Tests for GET /health.

The client fixture builds the real app without patching anything, so these
tests also prove the service starts without MongoDB, JWT or Ollama.
"""


def test_health_returns_200_with_ok_status(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
