from fastapi.testclient import TestClient


def test_landing_js_has_crlf_open_redirect_guard(client: TestClient) -> None:
    response = client.get("/static/landing.js")
    assert response.status_code == 200
    # CR/LF guard uses fromCharCode to dodge template-engine escaping concerns.
    assert 'String.fromCharCode(13)' in response.text
    assert 'String.fromCharCode(10)' in response.text
    # And the redirect must be same-origin (starts with single slash, not //).
    assert 'text.indexOf("/") === 0' in response.text
    assert 'text.indexOf("//") !== 0' in response.text


def test_auth_me_requires_authentication(client: TestClient) -> None:
    response = client.get("/auth/me")

    assert response.status_code == 401
    assert response.json()["detail"] == "Not authenticated"


def test_refresh_requires_refresh_cookie(client: TestClient) -> None:
    response = client.post("/auth/refresh")

    assert response.status_code == 401
    assert response.json()["detail"] == "No refresh token"
