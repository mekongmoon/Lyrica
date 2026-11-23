"""HTTP routes, grouped into Flask blueprints.

    core      /, /app, /favicon.ico
    lyrics    /lyrics/  (incl. mood, metadata, translation/romanization)
    metadata  /metadata/, /suggestion
    trending  /trending/, /analytics/*
    jiosaavn  /api/jiosaavn/*
    admin     /cache/*, /v2/proxy/*, /config/*

/health, /health/ready and /admin/cache/* are registered in src/app.py.
"""

from src.routes import admin, core, jiosaavn, lyrics, metadata, trending

BLUEPRINTS = (
    core.bp,
    lyrics.bp,
    metadata.bp,
    trending.bp,
    jiosaavn.bp,
    admin.bp,
)


def register_routes(app):
    """Register every route blueprint on the Flask app."""
    for blueprint in BLUEPRINTS:
        app.register_blueprint(blueprint)
