import json
import re
from datetime import datetime, timezone

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from app import db

ROLES = ("admin", "department")


def slugify(name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "dept"


class Department(db.Model):
    __tablename__ = "departments"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    slug = db.Column(db.String(120), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    users = db.relationship("User", back_populates="department")
    drones = db.relationship("Drone", back_populates="department")

    def __repr__(self):
        return f"<Department {self.name}>"


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    full_name = db.Column(db.String(120), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="department")
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id"), nullable=True)
    is_active_flag = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    department = db.relationship("Department", back_populates="users")

    def set_password(self, raw_password):
        self.password_hash = generate_password_hash(raw_password)

    def check_password(self, raw_password):
        return check_password_hash(self.password_hash, raw_password)

    @property
    def is_active(self):
        # Overrides UserMixin's default (always-True) property so a
        # deactivated account can no longer authenticate or stay logged in.
        return self.is_active_flag

    @property
    def is_admin(self):
        return self.role == "admin"

    def __repr__(self):
        return f"<User {self.username} ({self.role})>"


class Drone(db.Model):
    __tablename__ = "drones"

    id = db.Column(db.Integer, primary_key=True)
    msn = db.Column(db.String(80), unique=True, nullable=False, index=True)
    model = db.Column(db.String(80), nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id"), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="operational")
    notes = db.Column(db.Text, nullable=True)
    registered_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    department = db.relationship("Department", back_populates="drones")
    uploads = db.relationship(
        "Upload", back_populates="drone", order_by="Upload.uploaded_at.desc()"
    )

    def __repr__(self):
        return f"<Drone {self.msn} ({self.model})>"


class Upload(db.Model):
    __tablename__ = "uploads"

    id = db.Column(db.Integer, primary_key=True)
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    drone_id = db.Column(db.Integer, db.ForeignKey("drones.id"), nullable=True)

    original_filename = db.Column(db.String(255), nullable=False)
    archived_path = db.Column(db.String(500), nullable=True)

    drone_model = db.Column(db.String(80), nullable=True)
    msn_entered = db.Column(db.String(80), nullable=True)
    flight_date = db.Column(db.String(20), nullable=True)
    reported_by = db.Column(db.String(120), nullable=True)

    status = db.Column(db.String(20), nullable=False, default="queued")
    # queued -> processing -> completed | failed
    error_message = db.Column(db.Text, nullable=True)

    uploaded_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    analyzed_at = db.Column(db.DateTime, nullable=True)

    department = db.relationship("Department")
    user = db.relationship("User")
    drone = db.relationship("Drone", back_populates="uploads")
    analysis = db.relationship(
        "AnalysisResult", back_populates="upload", uselist=False,
        cascade="all, delete-orphan",
    )

    def __repr__(self):
        return f"<Upload {self.id} {self.original_filename} [{self.status}]>"


class AnalysisResult(db.Model):
    __tablename__ = "analysis_results"

    id = db.Column(db.Integer, primary_key=True)
    upload_id = db.Column(db.Integer, db.ForeignKey("uploads.id"), unique=True, nullable=False)

    report_status = db.Column(db.String(20), nullable=True)  # PASS / WARNING / CRITICAL
    alert_count = db.Column(db.Integer, nullable=False, default=0)
    top_alerts = db.Column(db.Text, nullable=True)  # JSON list, quick-glance only
    report_json = db.Column(db.Text, nullable=False)  # full process_log() output

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    upload = db.relationship("Upload", back_populates="analysis")

    def get_report(self):
        return json.loads(self.report_json)

    def get_top_alerts(self):
        return json.loads(self.top_alerts) if self.top_alerts else []


class MsnConflict(db.Model):
    """Recorded whenever an upload's MSN already belongs to a different
    department -- the upload is rejected and this is left for an admin to
    resolve rather than silently reassigning or merging drone records."""

    __tablename__ = "msn_conflicts"

    id = db.Column(db.Integer, primary_key=True)
    msn = db.Column(db.String(80), nullable=False)
    requesting_department_id = db.Column(db.Integer, db.ForeignKey("departments.id"), nullable=False)
    existing_drone_id = db.Column(db.Integer, db.ForeignKey("drones.id"), nullable=False)
    requested_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="pending")  # pending / reassigned / dismissed
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    resolved_at = db.Column(db.DateTime, nullable=True)

    requesting_department = db.relationship("Department", foreign_keys=[requesting_department_id])
    existing_drone = db.relationship("Drone", foreign_keys=[existing_drone_id])
    requested_by = db.relationship("User", foreign_keys=[requested_by_id])


class AuditLog(db.Model):
    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    action = db.Column(db.String(120), nullable=False)
    details = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    actor = db.relationship("User")

    @staticmethod
    def record(actor, action, details=""):
        entry = AuditLog(actor_id=actor.id if actor else None, action=action, details=details)
        db.session.add(entry)
