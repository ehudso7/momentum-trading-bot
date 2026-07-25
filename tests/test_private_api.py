"""Private-use boundary tests for the analytics service."""

from fastapi.testclient import TestClient

from trading_bot.api.server import app


def test_private_api_fails_closed_without_owner_key(monkeypatch) -> None:
    monkeypatch.setenv("TRADING_PRIVATE_MODE", "true")
    monkeypatch.delenv("TRADING_API_KEY", raising=False)
    client = TestClient(app)

    assert client.get("/health").status_code == 200
    assert client.get("/").status_code == 503


def test_private_api_rejects_anonymous_and_wrong_key(monkeypatch) -> None:
    monkeypatch.setenv("TRADING_PRIVATE_MODE", "true")
    monkeypatch.setenv("TRADING_API_KEY", "private-owner-key")
    client = TestClient(app)

    assert client.get("/").status_code == 401
    assert client.get(
        "/", headers={"Authorization": "Bearer wrong"}
    ).status_code == 401


def test_private_api_accepts_owner_key(monkeypatch) -> None:
    monkeypatch.setenv("TRADING_PRIVATE_MODE", "true")
    monkeypatch.setenv("TRADING_API_KEY", "private-owner-key")
    client = TestClient(app)

    response = client.get(
        "/", headers={"Authorization": "Bearer private-owner-key"}
    )
    assert response.status_code == 200
    assert "Analytics" in response.text or "Momentum" in response.text
