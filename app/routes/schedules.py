from flask import Blueprint, current_app, flash, redirect, render_template, url_for
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from wtforms import SelectField, StringField
from wtforms.validators import DataRequired, Length, Optional

from app.extensions import db
from app.models import AwsConnection, ScheduledBackup
from app.services.aws_connection_service import check_bucket_access
from app.services.job_runner import kick, run_scheduled_backup
from app.services.s3_sync import normalize_prefix
from app.services.schedule_service import FREQUENCIES
from app.utils import audit, is_valid_bucket_name, utcnow

schedules_bp = Blueprint("schedules", __name__, url_prefix="/schedules")

FREQUENCY_CHOICES = [
    ("HOURLY", "Every hour"),
    ("DAILY", "Every day"),
    ("WEEKLY", "Every week"),
]
STORAGE_CHOICES = [
    ("INTELLIGENT_TIERING", "Intelligent-Tiering (recommended: auto-saves, no retrieval fee)"),
    ("GLACIER_IR", "Glacier Instant Retrieval (cheapest for large, rarely-changing data)"),
    ("STANDARD_IA", "Standard-IA (cheap, 30-day minimum)"),
    ("STANDARD", "Standard (best for many tiny files)"),
]


class ScheduleForm(FlaskForm):
    name = StringField("Schedule name", validators=[DataRequired(), Length(max=180)])
    connection_id = SelectField("AWS account", coerce=int, validators=[DataRequired()])
    source_bucket = StringField("Bucket to protect", validators=[DataRequired(), Length(max=255)])
    source_prefix = StringField(
        "Folder / prefix (optional)", validators=[Optional(), Length(max=1024)]
    )
    frequency = SelectField("Frequency", choices=FREQUENCY_CHOICES, default="DAILY")
    storage_class = SelectField(
        "Backup storage class", choices=STORAGE_CHOICES, default="INTELLIGENT_TIERING"
    )


def _connected_connections():
    return AwsConnection.query.filter_by(owner_id=current_user.id, status="CONNECTED").all()


def _own_schedule(schedule_id):
    return ScheduledBackup.query.filter_by(
        id=schedule_id, owner_id=current_user.id
    ).first_or_404()


@schedules_bp.route("/")
@login_required
def list_schedules():
    connections = _connected_connections()
    form = ScheduleForm()
    form.connection_id.choices = [(c.id, f"{c.display_name} ({c.aws_account_id})") for c in connections]

    schedules = (
        ScheduledBackup.query.filter_by(owner_id=current_user.id)
        .order_by(ScheduledBackup.created_at.desc())
        .all()
    )
    return render_template(
        "schedules/list.html", schedules=schedules, connections=connections, form=form
    )


@schedules_bp.route("/create", methods=["POST"])
@login_required
def create_schedule():
    connections = _connected_connections()
    form = ScheduleForm()
    form.connection_id.choices = [(c.id, c.display_name) for c in connections]

    if not form.validate_on_submit():
        for field, errors in form.errors.items():
            for error in errors:
                flash(f"{getattr(form, field).label.text}: {error}", "danger")
        return redirect(url_for("schedules.list_schedules"))

    connection = next((c for c in connections if c.id == form.connection_id.data), None)
    if connection is None:
        flash("Choose a verified AWS account.", "danger")
        return redirect(url_for("schedules.list_schedules"))

    bucket = form.source_bucket.data.strip().lower()
    prefix = normalize_prefix(form.source_prefix.data)
    interval = FREQUENCIES[form.frequency.data]
    storage_class = form.storage_class.data

    error = None
    if not connection.backup_bucket:
        error = "Set a backup bucket for this AWS connection first."
    elif not is_valid_bucket_name(bucket):
        error = "Invalid bucket name."
    elif bucket == connection.backup_bucket:
        error = "The source bucket cannot be the backup bucket."
    elif not connection.scope_allows(bucket):
        error = (
            f"Bucket '{bucket}' is outside the source scope '{connection.source_scope}' "
            "granted in your CloudFormation stack. Update the stack's SourceBucketScope first."
        )
    elif interval < current_app.config["MIN_SCHEDULE_MINUTES"]:
        error = "That frequency is below the minimum allowed."
    elif storage_class not in current_app.config["ALLOWED_STORAGE_CLASSES"]:
        error = "Unsupported storage class."
    elif ScheduledBackup.query.filter_by(
        aws_connection_id=connection.id, source_bucket=bucket, source_prefix=prefix
    ).first():
        error = "A schedule for this bucket and prefix already exists."

    if error is None:
        ok, message = check_bucket_access(connection, bucket)
        if not ok:
            error = message
    if error:
        flash(error, "danger")
        return redirect(url_for("schedules.list_schedules"))

    schedule = ScheduledBackup(
        owner_id=current_user.id,
        aws_connection_id=connection.id,
        name=form.name.data.strip(),
        source_type="S3_BUCKET",
        source_bucket=bucket,
        source_prefix=prefix,
        destination_bucket=connection.backup_bucket,
        storage_class=storage_class,
        frequency=form.frequency.data,
        interval_minutes=interval,
        retention_days=connection.retention_days,
        enabled=True,
        next_run_at=utcnow(),
    )
    db.session.add(schedule)
    db.session.flush()
    audit("SCHEDULE_CREATED", "scheduled_backup", schedule.id)
    db.session.commit()

    kick(run_scheduled_backup, schedule.id)  # first backup starts immediately
    flash("Schedule created. The first backup is starting now.", "success")
    return redirect(url_for("schedules.list_schedules"))


@schedules_bp.route("/<int:schedule_id>/toggle", methods=["POST"])
@login_required
def toggle_schedule(schedule_id):
    schedule = _own_schedule(schedule_id)
    schedule.enabled = not schedule.enabled
    if schedule.enabled:
        schedule.next_run_at = utcnow()
    audit("SCHEDULE_ENABLED" if schedule.enabled else "SCHEDULE_PAUSED",
          "scheduled_backup", schedule.id)
    db.session.commit()
    flash("Schedule resumed." if schedule.enabled else "Schedule paused.", "info")
    return redirect(url_for("schedules.list_schedules"))


@schedules_bp.route("/<int:schedule_id>/run", methods=["POST"])
@login_required
def run_now(schedule_id):
    schedule = _own_schedule(schedule_id)
    if schedule.is_running:
        flash("This schedule is already running.", "warning")
    else:
        audit("SCHEDULE_RUN_NOW", "scheduled_backup", schedule.id)
        db.session.commit()
        kick(run_scheduled_backup, schedule.id, True)
        flash("Backup started. Refresh in a moment to see the result.", "info")
    return redirect(url_for("schedules.list_schedules"))


@schedules_bp.route("/<int:schedule_id>/delete", methods=["POST"])
@login_required
def delete_schedule(schedule_id):
    schedule = _own_schedule(schedule_id)
    audit("SCHEDULE_DELETED", "scheduled_backup", schedule.id)
    db.session.delete(schedule)
    db.session.commit()
    flash("Schedule deleted. Existing backups and recovery points were kept.", "warning")
    return redirect(url_for("schedules.list_schedules"))
