import uuid
from pathlib import Path

from flask import (
    Blueprint, current_app, flash, redirect, render_template, request, url_for
)
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from wtforms import (
    DateTimeLocalField, FloatField, SelectField, StringField, TextAreaField
)
from wtforms.validators import InputRequired, DataRequired, Length, NumberRange, Optional

from app.extensions import db
from app.models import AwsConnection, Incident, RecoveryJob
from app.services.emergency_recovery_service import (
    create_incident, get_eligible_recovery_points, start_recovery,
)
from app.services.job_runner import kick, run_recovery_job
from app.services.s3_backup_service import preserve_file_copy
from app.utils import audit, safe_filename, utcnow

incidents_bp = Blueprint("incidents", __name__, url_prefix="/incidents")


class IncidentForm(FlaskForm):
    title = StringField("Incident title", validators=[DataRequired(), Length(max=200)])
    incident_type = SelectField(
        "Incident type",
        choices=[
            ("ACCIDENTAL_DELETION", "Accidental deletion"),
            ("OBJECT_OVERWRITE", "Object overwrite"),
            ("RANSOMWARE", "Ransomware or suspected compromise"),
            ("DATABASE_CORRUPTION", "Database corruption"),
            ("APPLICATION_ERROR", "Application error"),
            ("HARDWARE_FAILURE", "Hardware failure"),
            ("CLOUD_FAILURE", "Cloud resource failure"),
        ],
        validators=[DataRequired()],
    )
    affected_resource = StringField(
        "Affected resource", validators=[DataRequired(), Length(max=300)]
    )
    severity = SelectField(
        "Severity",
        choices=[("LOW", "Low"), ("MEDIUM", "Medium"), ("HIGH", "High"), ("CRITICAL", "Critical")],
        validators=[DataRequired()],
    )
    reported_data_loss_percent = FloatField(
        "Reported data loss percentage",
        validators=[InputRequired(), NumberRange(min=0, max=100)],
    )
    discovered_at = DateTimeLocalField(
        "When was it discovered? (UTC, optional)",
        format="%Y-%m-%dT%H:%M",
        validators=[Optional()],
    )
    suspected_cause = TextAreaField("Suspected cause", validators=[Length(max=1000)])
    description = TextAreaField("Description", validators=[Length(max=2000)])


def _own_incident(incident_id):
    return Incident.query.filter_by(id=incident_id, owner_id=current_user.id).first_or_404()


@incidents_bp.route("/")
@login_required
def list_incidents():
    incidents = (
        Incident.query.filter_by(owner_id=current_user.id)
        .order_by(Incident.created_at.desc())
        .all()
    )
    connections = AwsConnection.query.filter_by(
        owner_id=current_user.id, status="CONNECTED"
    ).all()
    return render_template(
        "incidents/list.html", incidents=incidents, connections=connections, form=IncidentForm()
    )


@incidents_bp.route("/create/<int:connection_id>", methods=["POST"])
@login_required
def create(connection_id):
    connection = AwsConnection.query.filter_by(
        id=connection_id, owner_id=current_user.id, status="CONNECTED"
    ).first_or_404()

    form = IncidentForm()
    if not form.validate_on_submit():
        for field, errors in form.errors.items():
            for error in errors:
                flash(f"{getattr(form, field).label.text}: {error}", "danger")
        return redirect(url_for("incidents.list_incidents"))

    discovered_at = form.discovered_at.data
    if discovered_at and discovered_at > utcnow():
        flash("The discovery time cannot be in the future.", "danger")
        return redirect(url_for("incidents.list_incidents"))

    incident = create_incident(
        owner_id=current_user.id,
        connection_id=connection.id,
        form_data={
            "title": form.title.data,
            "incident_type": form.incident_type.data,
            "affected_resource": form.affected_resource.data,
            "severity": form.severity.data,
            "suspected_cause": form.suspected_cause.data,
            "description": form.description.data,
            "reported_data_loss_percent": form.reported_data_loss_percent.data,
            "discovered_at": discovered_at,
        },
    )

    flash("Incident recorded. Do not overwrite or delete potentially recoverable data.", "warning")
    return redirect(url_for("incidents.detail", incident_id=incident.id))


@incidents_bp.route("/<int:incident_id>")
@login_required
def detail(incident_id):
    incident = _own_incident(incident_id)
    recovery_points = get_eligible_recovery_points(current_user.id, incident.aws_connection_id)
    recovery_jobs = (
        RecoveryJob.query.filter_by(owner_id=current_user.id, incident_id=incident.id)
        .order_by(RecoveryJob.created_at.desc())
        .all()
    )
    # Best choice: the newest point taken at or before the incident was discovered.
    recommended_id = next(
        (p.id for p in recovery_points if p.recovery_point_time <= incident.discovered_at), None
    )
    return render_template(
        "incidents/detail.html",
        incident=incident,
        recovery_points=recovery_points,
        recovery_jobs=recovery_jobs,
        recommended_id=recommended_id,
        any_active=any(job.is_active for job in recovery_jobs),
    )


@incidents_bp.route("/<int:incident_id>/preserve", methods=["POST"])
@login_required
def preserve(incident_id):
    incident = _own_incident(incident_id)
    connection = incident.aws_connection

    uploaded_file = request.files.get("remaining_file")
    if not uploaded_file or not uploaded_file.filename:
        flash("Select a remaining data file to preserve.", "danger")
        return redirect(url_for("incidents.detail", incident_id=incident.id))
    if not connection.is_connected or not connection.backup_bucket:
        flash("The AWS connection must be verified and have a backup bucket.", "danger")
        return redirect(url_for("incidents.detail", incident_id=incident.id))

    temp_dir = Path(current_app.config["UPLOAD_DIR"]) / "emergency"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_path = temp_dir / f"{incident.id}_{uuid.uuid4().hex}_{safe_filename(uploaded_file.filename)}"
    uploaded_file.save(temp_path)

    try:
        preserve_file_copy(
            connection=connection,
            source_path=temp_path,
            incident_id=incident.id,
            display_name=safe_filename(uploaded_file.filename),
        )
        incident.status = "DATA_PRESERVED"
        audit("DATA_PRESERVED", "incident", incident.id)
        db.session.commit()
        flash("Remaining data was preserved in S3 and verified.", "success")
    except Exception:
        current_app.logger.exception("Emergency preservation failed")
        flash("Preservation failed. Do not delete the original remaining data.", "danger")
    finally:
        temp_path.unlink(missing_ok=True)

    return redirect(url_for("incidents.detail", incident_id=incident.id))


@incidents_bp.route("/<int:incident_id>/recover/<int:recovery_point_id>", methods=["POST"])
@login_required
def recover(incident_id, recovery_point_id):
    incident = _own_incident(incident_id)
    try:
        job = start_recovery(
            owner_id=current_user.id,
            incident_id=incident.id,
            recovery_point_id=recovery_point_id,
            destination=request.form.get("destination", ""),
        )
    except ValueError as error:
        flash(str(error), "danger")
        return redirect(url_for("incidents.detail", incident_id=incident.id))

    kick(run_recovery_job, job.id)
    flash("Recovery started. This page shows live progress.", "info")
    return redirect(url_for("incidents.detail", incident_id=incident.id))


@incidents_bp.route("/<int:incident_id>/close", methods=["POST"])
@login_required
def close(incident_id):
    incident = _own_incident(incident_id)
    incident.status = "CLOSED"
    audit("INCIDENT_CLOSED", "incident", incident.id)
    db.session.commit()
    flash("Incident closed.", "info")
    return redirect(url_for("incidents.detail", incident_id=incident.id))
