"""Backwards-compatibility shim.

Routes now live in the src.routes package as Flask blueprints, and the
async bridge lives in src.async_bridge. This module re-exports the names
that used to be defined here so existing imports keep working.
"""

from src.async_bridge import _close_thread_loops, run_async  # noqa: F401
from src.routes import register_routes  # noqa: F401
from src.routes._helpers import (  # noqa: F401
    _IS_PRODUCTION,
    _MAX_TEXT_LEN,
    _admin_check,
    _admin_key_ok,
    _error_details,
    _int_arg,
    _sanitize_text,
)
from src.routes.metadata import _SUGGESTION_CLIENT  # noqa: F401
from src.routes.trending import trending_engine  # noqa: F401
