"""Guard the public URL map.

Every route, its HTTP methods and its view-function name must stay stable:
clients depend on the URLs, and scripts/parse_flask_routes.py derives the
OpenAPI operationIds from the function names.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import create_app  # noqa: E402

EXPECTED_ROUTES = {
    # (rule, methods, view function name)
    ("/", ("GET",), "home"),
    ("/app", ("GET",), "app_page"),
    ("/favicon.ico", ("GET",), "favicon"),
    ("/health", ("GET",), "health_check"),
    ("/health/ready", ("GET",), "readiness_check"),
    ("/lyrics/", ("GET",), "lyrics"),
    ("/metadata/", ("GET",), "metadata"),
    ("/suggestion", ("GET",), "suggestion"),
    ("/trending/", ("GET",), "trending"),
    ("/analytics/top-queries/", ("GET",), "top_queries"),
    ("/analytics/trending-by-country/", ("GET",), "trending_by_country"),
    ("/analytics/trending-vs-queries/", ("GET",), "trending_vs_queries"),
    ("/analytics/trending-intersection/", ("GET",), "trending_intersection"),
    ("/api/jiosaavn/search", ("GET",), "jiosaavn_search"),
    ("/api/jiosaavn/play", ("GET",), "jiosaavn_play"),
    ("/cache/stats", ("GET",), "route_cache_stats"),
    ("/cache/clear", ("POST",), "route_clear_cache"),
    ("/admin/cache/stats", ("GET",), "admin_cache_stats"),
    ("/admin/cache/clear", ("POST",), "admin_clear_cache"),
    ("/v2/proxy/set", ("POST",), "proxy_set"),
    ("/v2/proxy/remove", ("DELETE", "POST"), "proxy_remove"),
    ("/v2/proxy/list", ("GET",), "proxy_list"),
    ("/v2/proxy/clear", ("POST",), "proxy_clear"),
    ("/config/status", ("GET",), "config_status"),
    ("/config/reload", ("POST",), "config_reload"),
    ("/v2/lyrics", ("GET",), "v2_lyrics"),
    ("/v2/capabilities", ("GET",), "v2_capabilities"),
    ("/v2/health", ("GET",), "v2_health"),
}


def _actual_routes():
    app = create_app()
    routes = set()
    for rule in app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        methods = tuple(sorted(rule.methods - {"HEAD", "OPTIONS"}))
        # Blueprint endpoints are "<blueprint>.<function>"; compare the function.
        routes.add((rule.rule, methods, rule.endpoint.rsplit(".", 1)[-1]))
    return routes


def test_url_map_is_unchanged():
    actual = _actual_routes()
    missing = EXPECTED_ROUTES - actual
    unexpected = actual - EXPECTED_ROUTES
    assert not missing and not unexpected, (
        f"missing routes: {sorted(missing)}\nunexpected routes: {sorted(unexpected)}"
    )
