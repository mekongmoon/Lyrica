"""Apple Music credentials and locale settings, resolved from the environment."""

import os

from src.config import (
    APPLE_LYRICS_LANGUAGE,
    APPLE_LYRICS_SCRIPT,
    APPLE_STOREFRONT,
)


def _resolve_developer_token() -> str | None:
    """Resolve Apple Music developer token from env (primary: explicit var)."""
    return (
        os.getenv("APPLE_MUSIC_DEVELOPER_TOKEN")
        or os.getenv("DEVELOPER_TOKEN")
    )


def _resolve_user_token() -> str | None:
    """Resolve Apple Music Music User Token from env."""
    return os.getenv("MUSIC_USER_TOKEN")


def _resolve_storefront() -> str:
    """Resolve storefront from env, defaulting to 'us'."""
    return (os.getenv("APPLE_STOREFRONT") or APPLE_STOREFRONT or "us").lower().strip()


def _resolve_lyrics_language() -> str:
    return (os.getenv("APPLE_LYRICS_LANGUAGE") or APPLE_LYRICS_LANGUAGE or "en-gb").strip().lower()


def _resolve_lyrics_script() -> str:
    return (os.getenv("APPLE_LYRICS_SCRIPT") or APPLE_LYRICS_SCRIPT or "en-Latn").strip()
