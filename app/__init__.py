import os

from flask import Flask
from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy

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

    app.register_blueprint(auth_bp)
    app.register_blueprint(dept_bp)
    app.register_blueprint(admin_bp, url_prefix="/admin")

    with app.app_context():
        db.create_all()
        _bootstrap_admin(app)

    return app


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
