import os
import sqlite3
import time

import click
from flask import Flask, render_template
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError, ProgrammingError

from app.config import INSECURE_SECRETS, config_by_name
from app.extensions import csrf, db, login_manager, migrate
from app.utils import fmt_dt, human_size


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record):
    """SQLite: enforce foreign keys, allow concurrent web+worker access."""
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass  # e.g. in-memory databases
        cursor.close()


def create_tables_with_retry(attempts: int = 5):
    for attempt in range(attempts):
        try:
            db.create_all()
            return
        except (OperationalError, ProgrammingError):
            # Two processes starting at once can race on CREATE TABLE.
            if attempt == attempts - 1:
                raise
            time.sleep(1 + attempt)


def create_app(config_name: str | None = None, init_db: bool = True) -> Flask:
    config_name = config_name or os.getenv("FLASK_ENV", "development")
    app = Flask(__name__)
    app.config.from_object(config_by_name[config_name])

    if config_name == "production" and app.config["SECRET_KEY"] in INSECURE_SECRETS:
        raise RuntimeError(
            "SECRET_KEY is not set. Generate one with: "
            'python -c "import secrets; print(secrets.token_hex(32))"'
        )

    app.config["UPLOAD_DIR"].mkdir(parents=True, exist_ok=True)
    app.config["RESTORE_DIR"].mkdir(parents=True, exist_ok=True)

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    csrf.init_app(app)

    from app import models  # noqa: F401  (registers tables)
    from app.routes.auth import auth_bp
    from app.routes.aws_connections import aws_connections_bp
    from app.routes.backups import backups_bp
    from app.routes.dashboard import dashboard_bp
    from app.routes.incidents import incidents_bp
    from app.routes.schedules import schedules_bp

    for blueprint in (
        auth_bp, dashboard_bp, aws_connections_bp, backups_bp, schedules_bp, incidents_bp
    ):
        app.register_blueprint(blueprint)

    app.jinja_env.filters["filesize"] = human_size
    app.jinja_env.filters["dt"] = fmt_dt

    if init_db and app.config["AUTO_CREATE_TABLES"]:
        with app.app_context():
            create_tables_with_retry()
            # gunicorn --preload forks after this point: never share DB connections across fork.
            if not app.config.get("TESTING"):  # would wipe the in-memory test DB
                db.engine.dispose()

    @app.route("/health")
    def health():
        try:
            db.session.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001
            return {"status": "degraded", "service": "cloudshield-dr"}, 503
        return {"status": "ok", "service": "cloudshield-dr"}, 200

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "img-src 'self' data:; frame-ancestors 'none'; form-action 'self'; base-uri 'self'",
        )
        if app.config["SESSION_COOKIE_SECURE"]:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response

    def error_page(code, title, message):
        return render_template("errors/error.html", code=code, title=title, message=message), code

    @app.errorhandler(403)
    def forbidden(_e):
        return error_page(403, "Access denied", "You don't have permission to view this page.")

    @app.errorhandler(404)
    def not_found(_e):
        return error_page(404, "Not found", "That page or record doesn't exist.")

    @app.errorhandler(413)
    def too_large(_e):
        limit = app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)
        return error_page(413, "File too large", f"Uploads are limited to {limit} MB.")

    @app.errorhandler(500)
    def server_error(_e):
        db.session.rollback()
        return error_page(500, "Something went wrong", "The error was logged. Please try again.")

    @app.cli.command("init-db")
    def init_db_command():
        """Create all database tables."""
        create_tables_with_retry()
        click.echo("Database tables are ready.")

    return app
