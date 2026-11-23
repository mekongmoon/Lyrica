"""Request helpers shared by the route blueprints."""

import hmac
import os
import re

from flask import jsonify, request

from src.config import ADMIN_KEY

_IS_PRODUCTION = os.getenv("LYRICA_ENV", "").lower() == "production"
_MAX_TEXT_LEN = int(os.getenv("MAX_TEXT_LENGTH", "200"))


def _error_details(exc: BaseException) -> dict:
    """Only expose raw exception text outside production.

    Upstream exception messages can contain internal hostnames, tokens, or
    filesystem paths. Clients still get the stable error envelope.
    """
    if _IS_PRODUCTION:
        return {}
    return {"details": str(exc)}


def _admin_key_ok(key: str | None) -> bool:
    """Constant-time admin key comparison."""
    if not ADMIN_KEY or not key:
        return False
    return hmac.compare_digest(str(key), str(ADMIN_KEY))


def _sanitize_text(value: str, max_len: int = _MAX_TEXT_LEN) -> str:
    """Strip control characters and bound user-controlled text fields."""
    value = re.sub(r"[\x00-\x1f\x7f]", " ", (value or "")).strip()
    return value[:max_len]


def _int_arg(name: str, default: int, minimum: int, maximum: int) -> int:
    """Parse a bounded integer query parameter without ever raising a 500."""
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def _admin_check():
    """Return (True, None) if admin key is valid, else (False, response)."""
    key = request.headers.get("X-ADMIN-KEY") or request.args.get("key")
    if not _admin_key_ok(key):
        return False, (jsonify({"status": "error", "error": {"message": "Unauthorized — admin key required"}}), 403)
    return True, None
