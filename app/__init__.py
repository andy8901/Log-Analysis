import os

from flask import Flask, jsonify
from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text

from config import Config

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message_category = "error"


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def create_app(config_object=Config):
    # templates/ and static/ live at the project root (alongside analyzer.py,
    # reports.py, ...), not inside this app/ package, so Flask's defaults
    # (relative to this package) have to be overridden explicitly.
    app = Flask(
        __name__,
        template_folder=os.path.join(PROJECT_ROOT, "templates"),
        static_folder=os.path.join(PROJECT_ROOT, "static"),
    )
    app.config.from_object(config_object)

    os.makedirs(os.path.join(PROJECT_ROOT, "instance"), exist_ok=True)
    os.makedirs(app.config["ARCHIVE_ROOT"], exist_ok=True)
    os.makedirs(app.config["UPLOAD_TMP"], exist_ok=True)

    db.init_app(app)
    login_manager.init_app(app)

    from app.models import User

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from app.auth import auth_bp
    from app.routes_dept import dept_bp
    from app.routes_admin import admin_bp
    from app.api import api_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(dept_bp)
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(api_bp)

    @app.context_processor
    def inject_app_version():
        return {"app_version": config_object.APP_VERSION}

    @app.route("/healthz")
    def healthz():
        # Unauthenticated on purpose -- a monitoring probe, auto-restart
        # script, or load balancer health check shouldn't need a login.
        # Actually runs a query rather than just returning 200 unconditionally,
        # so a broken DB connection/disk shows up as unhealthy, not a false OK.
        try:
            db.session.execute(text("SELECT 1"))
            return jsonify(status="ok"), 200
        except Exception as exc:
            app.logger.exception("Health check failed")
            return jsonify(status="error", detail=str(exc)), 503

    with app.app_context():
        db.create_all()
        _migrate_add_missing_columns(app)
        _bootstrap_admin(app)

    return app


def _migrate_add_missing_columns(app):
    """db.create_all() only creates tables that don't exist yet -- it never
    alters an existing table, so a model gaining a new column (as happens
    whenever this app's data model grows) would otherwise need the whole
    database deleted and re-bootstrapped. There's no Alembic wired up here,
    so this does the one thing that actually matters for a SQLite column
    addition: ALTER TABLE ... ADD COLUMN, run once per missing column,
    skipped entirely once every column already exists. Safe by
    construction -- every column this adds is nullable, so existing rows
    just get NULL there; nothing here ever drops or renames a column."""
    inspector = inspect(db.engine)
    for table in db.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        existing_columns = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing_columns:
                continue
            col_type = column.type.compile(dialect=db.engine.dialect)
            app.logger.warning(
                "Migrating: adding column %s.%s (%s)", table.name, column.name, col_type
            )
            with db.engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}'))


def _bootstrap_admin(app):
    """Create a default admin account on first run so the platform is usable
    before anyone has logged in to create further users. No-op once at least
    one user exists."""
    from app.models import User

    if User.query.first() is not None:
        return

    admin = User(
        username=app.config["ADMIN_USERNAME"],
        full_name="Administrator",
        role="admin",
        department_id=None,
    )
    admin.set_password(app.config["ADMIN_PASSWORD"])
    db.session.add(admin)
    db.session.commit()
    app.logger.warning(
        "Bootstrapped default admin user '%s' with the configured ADMIN_PASSWORD "
        "env var (or the insecure built-in default if that wasn't set) -- "
        "log in and change it, or set ADMIN_USERNAME/ADMIN_PASSWORD before first run.",
        admin.username,
    )
