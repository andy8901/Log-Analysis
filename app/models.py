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
    __table_args__ = (
        # MSN is unique per department, not globally -- different
        # departments are free to use the same MSN for their own separate
        # drone record (e.g. each has its own "DRONE-001").
        db.UniqueConstraint("department_id", "msn", name="uq_drone_department_msn"),
    )

    id = db.Column(db.Integer, primary_key=True)
    msn = db.Column(db.String(80), nullable=False, index=True)
    model = db.Column(db.String(80), nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id"), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="operational")
    notes = db.Column(db.Text, nullable=True)
    registered_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    # Who this physical drone is deployed with -- "Neosky" (or left blank)
    # for an in-house/factory unit, otherwise the external customer's name.
    # Set from the "Customer" field on the upload form (see routes_dept.
    # process_and_save_log()); independent of department, since a customer
    # is who the drone was sold to/deployed with, not which internal
    # Neosky department happened to upload its logs.
    customer_name = db.Column(db.String(200), nullable=True)

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

    # "upload" (a real .bin/.log/.tlog processed by analyzer.py, the normal
    # path) or "imported" (a row backfilled by an admin from a historical
    # Multilog-format Excel export -- see routes_admin.import_raw_data()).
    # Imported rows have no raw log and no analyzer.py report, so the UI
    # hides report/3D/export actions that need one.
    source = db.Column(db.String(20), nullable=False, default="upload")

    # Root-cause conclusion, required before downloads (Word/Excel/3D video)
    # unlock on the report page -- CONCLUSION_CHOICES below is the fixed
    # set; "other" pairs with conclusion_note for free text.
    conclusion = db.Column(db.String(20), nullable=True)
    conclusion_note = db.Column(db.Text, nullable=True)
    concluded_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    concluded_at = db.Column(db.DateTime, nullable=True)

    uploaded_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    analyzed_at = db.Column(db.DateTime, nullable=True)

    department = db.relationship("Department")
    user = db.relationship("User", foreign_keys=[user_id])
    drone = db.relationship("Drone", back_populates="uploads")
    concluded_by = db.relationship("User", foreign_keys=[concluded_by_id])
    analysis = db.relationship(
        "AnalysisResult", back_populates="upload", uselist=False,
        cascade="all, delete-orphan",
    )

    @property
    def has_conclusion(self):
        return self.conclusion is not None

    def __repr__(self):
        return f"<Upload {self.id} {self.original_filename} [{self.status}]>"


# Fixed set of root-cause conclusions a report can be closed out with,
# required before its downloads unlock -- (value, label).
CONCLUSION_CHOICES = (
    ("pilot_error", "Pilot error"),
    ("system_error", "System error"),
    ("undetermined", "Yet to decide"),
    ("other", "Other"),
)


class AnalysisResult(db.Model):
    __tablename__ = "analysis_results"

    id = db.Column(db.Integer, primary_key=True)
    upload_id = db.Column(db.Integer, db.ForeignKey("uploads.id"), unique=True, nullable=False)

    report_status = db.Column(db.String(20), nullable=True)  # PASS / WARNING / CRITICAL
    alert_count = db.Column(db.Integer, nullable=False, default=0)
    top_alerts = db.Column(db.Text, nullable=True)  # JSON list, quick-glance only
    report_json = db.Column(db.Text, nullable=False)  # full process_log() output

    # Denormalized out of report_json at upload time, purely so admin
    # aggregate views (per-drone/per-department/per-person averages) can use
    # plain SQL instead of deserializing every report on every dashboard
    # load. Derived values only -- process_log()'s own output is untouched.
    flight_duration_min = db.Column(db.Float, nullable=True)
    battery_sag_pct = db.Column(db.Float, nullable=True)
    distance_travelled_m = db.Column(db.Float, nullable=True)
    avg_altitude_m = db.Column(db.Float, nullable=True)
    avg_battery_voltage_v = db.Column(db.Float, nullable=True)
    avg_current_a = db.Column(db.Float, nullable=True)

    # The same ~150-field extraction multilog_analyzer.analyze_single_log()
    # produces for the admin Multilog Analysis export -- captured once here
    # at upload time (best-effort; stays null if extraction fails) so the
    # richer per-field dashboard metrics (range from home, battery voltage
    # at a failsafe trigger, thrust/GPS issue counts -- fields that live
    # here, not in process_log()'s own report) don't require re-parsing the
    # raw log on every dashboard load. For an admin-imported historical row
    # (Upload.source == "imported"), this *is* the row's data verbatim --
    # there's no separate process_log() report behind it.
    multilog_json = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    upload = db.relationship("Upload", back_populates="analysis")

    def get_report(self):
        return json.loads(self.report_json)

    def get_top_alerts(self):
        return json.loads(self.top_alerts) if self.top_alerts else []

    def get_multilog(self):
        return json.loads(self.multilog_json) if self.multilog_json else None


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


class Warranty(db.Model):
    """Bulk-imported from the external warranty tracker spreadsheet (see
    routes_admin.import_warranty()) -- deliberately its own table, not a
    column on Drone, because the tracker covers every drone ever sold
    (including ones with no Upload/Drone record in this app yet, since
    plenty of customer units never have a log run through here) and has no
    department of its own (a customer isn't a Neosky department). Keyed on
    (msn, customer_name) rather than msn alone, since the same MSN number
    can legitimately recur across different customers in the tracker.

    Expiry/days-left/status/drone-age are deliberately NOT stored columns
    -- they're relative to "today" and would go stale the moment they
    were written; app/warranty.py computes them fresh from purchase_date +
    warranty_months whenever they're displayed."""
    __tablename__ = "warranties"
    __table_args__ = (
        db.UniqueConstraint("msn", "customer_name", name="uq_warranty_msn_customer"),
    )

    id = db.Column(db.Integer, primary_key=True)
    msn = db.Column(db.String(80), nullable=False, index=True)
    customer_name = db.Column(db.String(200), nullable=False)
    product_name = db.Column(db.String(80), nullable=True)
    purchase_date = db.Column(db.Date, nullable=True)
    warranty_months = db.Column(db.Integer, nullable=True)
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<Warranty {self.customer_name}/{self.msn}>"
