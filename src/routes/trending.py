"""GET /trending/ and GET /analytics/* — charts and user-query analytics."""

from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from src.logger import get_logger
from src.routes._helpers import _error_details, _int_arg, _sanitize_text
from src.trending_analytics import Country, TrendingAnalyticsEngine

logger = get_logger("router")

# Shared with the lyrics blueprint, which records user queries.
trending_engine = TrendingAnalyticsEngine(cache_ttl_hours=24)

bp = Blueprint("trending", __name__)


@bp.route("/trending/", methods=["GET"])
def trending():
    """Get trending songs by country"""
    country = _sanitize_text(request.args.get("country", "US"), 8).upper() or "US"
    countries_param = _sanitize_text(request.args.get("countries", ""), 64)
    limit = _int_arg("limit", 20, 1, 100)

    logger.info(f"Trending request: country={country}, limit={limit}")

    try:
        # Handle single country
        if country and not countries_param:
            try:
                country_enum = Country[country]
                trending_songs = trending_engine.fetch_trending_songs(country_enum, limit)

                return jsonify({
                    "status": "success",
                    "data": {
                        "country": country,
                        "trending": [song.to_dict() for song in trending_songs],
                        "total": len(trending_songs),
                        "timestamp": datetime.now(timezone.utc).isoformat()
                    }
                })
            except KeyError:
                return jsonify({
                    "status": "error",
                    "error": {
                        "message": f"Invalid country code: {country}",
                        "valid_countries": [c.value for c in Country],
                        "timestamp": datetime.now(timezone.utc).isoformat()
                    }
                }), 400

        # Handle multiple countries
        elif countries_param:
            country_list = [c.strip().upper() for c in countries_param.split(",")]
            trending_data = {}

            for c in country_list:
                try:
                    country_enum = Country[c]
                    trending_songs = trending_engine.fetch_trending_songs(country_enum, limit)
                    trending_data[c] = [song.to_dict() for song in trending_songs]
                except KeyError:
                    logger.warning(f"Invalid country code: {c}")
                    continue

            return jsonify({
                "status": "success",
                "data": {
                    "countries": trending_data,
                    "timestamp": datetime.now(timezone.utc).isoformat()
                }
            })

    except Exception as e:
        logger.error(f"Trending fetch error: {str(e)}")
        return jsonify({
            "status": "error",
            "error": {
                "message": "Failed to fetch trending data",
                **_error_details(e),
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
        }), 500

@bp.route("/analytics/top-queries/", methods=["GET"])
def top_queries():
    """Get top user queries globally or by country"""
    limit = _int_arg("limit", 20, 1, 100)
    country = _sanitize_text(request.args.get("country", ""), 8).upper()
    days_raw = request.args.get("days")
    days = _int_arg("days", 0, 0, 3650) if days_raw else None

    if days is not None and days <= 0:
        days = None

    logger.info(f"Top queries request: limit={limit}, country={country}, days={days}")

    try:
        top_q = trending_engine.get_top_queries(
            limit=limit,
            country=country if country else None,
            days=days
        )

        return jsonify({
            "status": "success",
            "data": {
                "scope": "global" if not country else f"country_{country}",
                "time_window": f"{days} days" if days else "all_time",
                "top_queries": [{"query": q, "count": c} for q, c in top_q],
                "total_unique": len(top_q),
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
        })

    except Exception as e:
        logger.error(f"Top queries fetch error: {str(e)}")
        return jsonify({
            "status": "error",
            "error": {
                "message": "Failed to fetch top queries",
                **_error_details(e),
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
        }), 500

@bp.route("/analytics/trending-by-country/", methods=["GET"])
def trending_by_country():
    """Get top queries for each country"""
    limit = _int_arg("limit", 10, 1, 100)

    logger.info(f"Trending by country request: limit={limit}")

    try:
        top_by_country = trending_engine.get_top_queries_by_country(limit=limit)

        return jsonify({
            "status": "success",
            "data": {
                "countries": {
                    country: [{"query": q, "count": c} for q, c in queries]
                    for country, queries in top_by_country.items()
                },
                "total_countries": len(top_by_country),
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
        })

    except Exception as e:
        logger.error(f"Trending by country error: {str(e)}")
        return jsonify({
            "status": "error",
            "error": {
                "message": "Failed to fetch trending by country",
                **_error_details(e),
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
        }), 500

@bp.route("/analytics/trending-vs-queries/", methods=["GET"])
def trending_vs_queries():
    """Compare trending songs with top user queries"""
    country = _sanitize_text(request.args.get("country", "US"), 8).upper() or "US"
    limit = _int_arg("limit", 10, 1, 100)

    logger.info(f"Trending vs queries request: country={country}, limit={limit}")

    try:
        country_enum = Country[country]
        comparison = trending_engine.get_trending_vs_user_queries(country_enum, limit)

        return jsonify({
            "status": "success",
            "data": comparison
        })

    except KeyError:
        return jsonify({
            "status": "error",
            "error": {
                "message": f"Invalid country code: {country}",
                "valid_countries": [c.value for c in Country],
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
        }), 400
    except Exception as e:
        logger.error(f"Trending vs queries error: {str(e)}")
        return jsonify({
            "status": "error",
            "error": {
                "message": "Failed to fetch trending vs queries",
                **_error_details(e),
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
        }), 500

@bp.route("/analytics/trending-intersection/", methods=["GET"])
def trending_intersection():
    """Find queries that match trending songs"""
    country = _sanitize_text(request.args.get("country", "US"), 8).upper() or "US"
    limit = _int_arg("limit", 10, 1, 100)

    logger.info(f"Trending intersection request: country={country}, limit={limit}")

    try:
        country_enum = Country[country]
        matches = trending_engine.get_trending_intersection(country_enum, limit)

        return jsonify({
            "status": "success",
            "data": {
                "country": country,
                "matches": matches,
                "total_matches": len(matches),
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
        })

    except KeyError:
        return jsonify({
            "status": "error",
            "error": {
                "message": f"Invalid country code: {country}",
                "valid_countries": [c.value for c in Country],
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
        }), 400
    except Exception as e:
        logger.error(f"Trending intersection error: {str(e)}")
        return jsonify({
            "status": "error",
            "error": {
                "message": "Failed to fetch trending intersection",
                **_error_details(e),
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
        }), 500
