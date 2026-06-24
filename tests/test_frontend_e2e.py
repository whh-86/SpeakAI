import pytest


playwright = pytest.importorskip("playwright.sync_api")


def test_frontend_shell_serves_controls(tmp_path, monkeypatch):
    from app import create_app

    monkeypatch.setenv("SPEAKAI_DB_PATH", str(tmp_path / "speakai-test.db"))
    app = create_app()
    app.config.update(TESTING=True)

    with app.test_client() as client:
        response = client.get("/")
        html = response.data.decode("utf-8")

    assert response.status_code == 200
    assert 'id="btn-mic"' in html
    assert 'id="btn-generate"' in html
    assert 'id="voice-select"' in html
    assert 'id="asr-model-select"' in html
    assert 'id="prompt-textarea"' in html
    assert 'id="greeting-textarea"' in html
    assert 'show-6' in html
    assert "/api/chat" in html or "app.js" in html
