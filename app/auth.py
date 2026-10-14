from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

from app.models import AuditLog, User
from app import db

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dept.dashboard") if not current_user.is_admin else url_for("admin.dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        user = User.query.filter_by(username=username).first()

        if user and user.is_active and user.check_password(password):
            login_user(user)
            AuditLog.record(user, "login")
            db.session.commit()
            return redirect(url_for("admin.dashboard") if user.is_admin else url_for("dept.dashboard"))

        flash("Invalid username or password", "error")

    return render_template("login.html")


@auth_bp.route("/logout")
@login_required
def logout():
    AuditLog.record(current_user, "logout")
    db.session.commit()
    logout_user()
    return redirect(url_for("auth.login"))
