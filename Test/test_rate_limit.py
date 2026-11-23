"""Rate limiting works end to end with the pinned Flask-Limiter / limits.

conftest.py disables rate limiting for the rest of the suite, so this test
re-enables it for one app instance (in-memory storage, development limit of
15 requests per minute).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import create_app  # noqa: E402

DEV_LIMIT = 15


def test_rate_limit_enforced_with_headers_and_health_exempt(monkeypatch):
    monkeypatch.setenv("RATELIMIT_ENABLED", "true")
    monkeypatch.delenv("LYRICA_ENV", raising=False)
    monkeypatch.setenv("RATE_LIMIT_STORAGE_URI", "memory://")
    client = create_app().test_client()

    first = client.get("/")
    assert first.status_code == 200
    assert first.headers.get("X-RateLimit-Limit") == str(DEV_LIMIT)

    for _ in range(DEV_LIMIT - 1):
        assert client.get("/").status_code == 200

    limited = client.get("/")
    assert limited.status_code == 429
    assert limited.get_json()["status"] == "error"
    assert int(limited.headers["Retry-After"]) > 0

    # Health checks are exempt so orchestrators never see 429.
    assert client.get("/health").status_code == 200
