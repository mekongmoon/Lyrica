"""Cache, proxy-pool and config endpoints.

/cache/stats and /config/status are public; the rest require the admin key
(X-ADMIN-KEY header, or ?key= for legacy clients). The /admin/cache/*
endpoints and /health live in src/app.py.
"""

from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from src.cache import cache_stats, clear_cache
from src.logger import get_logger
from src.proxy_manager import get_proxy_manager
from src.routes._helpers import _admin_check, _admin_key_ok, _error_details, _sanitize_text
from src.user_config import get_user_config, reload_user_config

logger = get_logger("router")

bp = Blueprint("admin", __name__)


@bp.route("/cache/stats", methods=["GET"])
def route_cache_stats():
    """Get cache statistics and information"""
    try:
        stats = cache_stats()
        # Public endpoint: do not disclose host filesystem paths.
        stats.pop("cache_dir", None)
        return jsonify({"status": "success", **stats})
    except Exception as e:
        logger.error(f"Cache stats error: {str(e)}")
        return (
            jsonify({
                "status": "error",
                "error": {
                    "message": "Failed to retrieve cache stats",
                    **_error_details(e),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            }),
            500,
        )

@bp.route("/cache/clear", methods=["POST"])
def route_clear_cache():
    """Clear all cached data (Admin only)"""
    # Require admin key via header (preferred) or query param (legacy)
    key = request.headers.get("X-ADMIN-KEY") or request.args.get("key")
    if not _admin_key_ok(key):
        return (
            jsonify({"status": "error", "error": {"message": "Unauthorized"}}),
            403,
        )
    try:
        res = clear_cache()
        logger.info("Cache cleared")
        return jsonify({"status": "success", "details": res})
    except Exception as e:
        logger.error(f"Cache clear error: {str(e)}")
        return (
            jsonify({
                "status": "error",
                "error": {
                    "message": "Failed to clear cache",
                    **_error_details(e),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            }),
            500,
        )


# ── Proxy management (admin-key protected) ──────────────────────────────────


@bp.route("/v2/proxy/set", methods=["POST"])
def proxy_set():
    """Add a proxy to the pool (admin-key protected)"""
    ok, err = _admin_check()
    if not ok:
        return err

    body = request.get_json(silent=True) or {}
    proxy_url = _sanitize_text(body.get("proxy") or request.args.get("proxy", ""), 2048)

    if not proxy_url:
        return jsonify({"status": "error", "error": {"message": "`proxy` field is required"}}), 400

    added = get_proxy_manager().add(proxy_url)
    return jsonify({
        "status":  "success" if added else "skipped",
        "message": "Proxy added" if added else "Proxy already in pool or invalid",
        "pool_size": get_proxy_manager().size(),
    })

@bp.route("/v2/proxy/remove", methods=["DELETE", "POST"])
def proxy_remove():
    """Remove a proxy from the pool by URL (admin-key protected)"""
    ok, err = _admin_check()
    if not ok:
        return err

    body = request.get_json(silent=True) or {}
    proxy_url = _sanitize_text(body.get("proxy") or request.args.get("proxy", ""), 2048)

    if not proxy_url:
        return jsonify({"status": "error", "error": {"message": "`proxy` field is required"}}), 400

    removed = get_proxy_manager().remove(proxy_url)
    return jsonify({
        "status":  "success" if removed else "not_found",
        "message": "Proxy removed" if removed else "Proxy not found in pool",
        "pool_size": get_proxy_manager().size(),
    })

@bp.route("/v2/proxy/list", methods=["GET"])
def proxy_list():
    """List all proxies with masked credentials (admin-key protected)"""
    ok, err = _admin_check()
    if not ok:
        return err

    return jsonify({
        "status":  "success",
        "pool_size": get_proxy_manager().size(),
        "proxies": get_proxy_manager().list_masked(),
    })

@bp.route("/v2/proxy/clear", methods=["POST"])
def proxy_clear():
    """Remove all proxies from the pool (admin-key protected)"""
    ok, err = _admin_check()
    if not ok:
        return err

    count = get_proxy_manager().clear()
    return jsonify({
        "status":  "success",
        "message": f"{count} proxies removed",
        "pool_size": 0,
    })


# ── Config management ───────────────────────────────────────────────────────


@bp.route("/config/status", methods=["GET"])
def config_status():
    """Show currently loaded config file and effective settings"""
    try:
        cfg = get_user_config()
        return jsonify({
            "status": "success",
            "config": cfg.to_dict(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        logger.error(f"Config status error: {e}")
        return jsonify({"status": "error", "error": {"message": str(e)}}), 500

@bp.route("/config/reload", methods=["POST"])
def config_reload():
    """Force config file reload without restarting the server (admin-key protected)"""
    ok, err = _admin_check()
    if not ok:
        return err

    try:
        cfg = reload_user_config()
        return jsonify({
            "status":  "success",
            "message": "Config reloaded",
            "config":  cfg.to_dict(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        logger.error(f"Config reload error: {e}")
        return jsonify({"status": "error", "error": {"message": str(e)}}), 500
