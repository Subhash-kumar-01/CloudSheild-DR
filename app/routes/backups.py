from pathlib import Path

from flask import (
    Blueprint, abort, current_app, flash, redirect, render_template, request, send_file, url_for
)
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from flask_wtf.file import FileField, FileRequired
from wtforms import StringField, TextAreaField
from wtforms.validators import DataRequired, Length

from app.extensions import db
from app.models import AwsConnection, BackupJob, RecoveryJob, RecoveryPoint
from app.services.emergency_recovery_service import start_recovery
from app.services.job_runner import kick, run_recovery_job
from app.services.s3_backup_service import create_backup_from_upload, list_object_versions
from app.utils import audit, safe_filename

backups_bp = Blueprint("backups", __name__, url_prefix="/backups")


class UploadBackupForm(FlaskForm):
    backup_name = StringField("Backup name", validators=[DataRequired(), Length(max=180)])
    description = TextAreaField("Description", validators=[Length(max=1000)])
    file = FileField("Backup file", validators=[FileRequired()])


@backups_bp.route("/")
@login_required
def list_backups():
    backups = (
        BackupJob.query.filter_by(owner_id=current_user.id)
        .order_by(BackupJob.created_at.desc())
        .limit(200)
        .all()
    )
    connections = AwsConnection.query.filter_by(
        owner_id=current_user.id, status="CONNECTED"
    ).all()
    return render_template(
        "backups/list.html",
        backups=backups,
        connections=connections,
        form=UploadBackupForm(),
    )


@backups_bp.route("/upload/<int:connection_id>", methods=["POST"])
@login_required
def upload_backup(connection_id):
    connection = AwsConnection.query.filter_by(
        id=connection_id, owner_id=current_user.id, status="CONNECTED"
    ).first_or_404()

    form = UploadBackupForm()
    if not form.validate_on_submit():
        flash("Please provide a backup name and file.", "danger")
        return redirect(url_for("backups.list_backups"))

    uploaded_file = form.file.data
    extension = Path(safe_filename(uploaded_file.filename)).suffix.lower()
    if extension not in current_app.config["ALLOWED_EXTENSIONS"]:
        flash("This file type is not allowed.", "danger")
        return redirect(url_for("backups.list_backups"))

    if not connection.backup_bucket:
        flash("Set a backup bucket for this AWS connection first.", "danger")
        return redirect(url_for("aws_connections.connection_detail", connection_id=connection.id))

    try:
        backup = create_backup_from_upload(
            connection=connection,
            uploaded_file=uploaded_file,
            backup_name=form.backup_name.data.strip(),
            description=form.description.data,
        )
        audit("BACKUP_UPLOADED", "backup_job", backup.id)
        db.session.commit()
        flash("Backup uploaded and verified successfully.", "success")
    except Exception:
        current_app.logger.exception("Backup upload failed")
        flash("Backup failed. Check the backup history for the recorded error.", "danger")

    return redirect(url_for("backups.list_backups"))


@backups_bp.route("/<int:backup_id>")
@login_required
def backup_detail(backup_id):
    backup = BackupJob.query.filter_by(id=backup_id, owner_id=current_user.id).first_or_404()

    versions = []
    if backup.status == "SUCCEEDED" and backup.object_key:
        try:
            versions = list_object_versions(
                backup.aws_connection, backup.destination_bucket, backup.object_key
            )
        except Exception:
            current_app.logger.exception("Unable to list object versions")

    recovery_point = RecoveryPoint.query.filter_by(
        backup_job_id=backup.id, owner_id=current_user.id
    ).first()
    return render_template(
        "backups/detail.html",
        backup=backup,
        versions=versions,
        recovery_point=recovery_point,
        restore_prefix=current_app.config["RESTORE_PREFIX"],
    )


@backups_bp.route("/<int:backup_id>/restore", methods=["POST"])
@login_required
def restore(backup_id):
    backup = BackupJob.query.filter_by(id=backup_id, owner_id=current_user.id).first_or_404()

    if backup.status != "SUCCEEDED":
        flash("Only successful backups can be restored.", "danger")
        return redirect(url_for("backups.backup_detail", backup_id=backup.id))

    recovery_point = RecoveryPoint.query.filter_by(
        backup_job_id=backup.id, owner_id=current_user.id
    ).first()
    if recovery_point is None:
        flash(
            "This run found nothing new to back up, so it has no recovery point of its own. "
            "Restore from the most recent run that copied data.",
            "warning",
        )
        return redirect(url_for("backups.backup_detail", backup_id=backup.id))

    try:
        job = start_recovery(
            owner_id=current_user.id,
            incident_id=None,
            recovery_point_id=recovery_point.id,
            destination=request.form.get("destination", ""),
        )
    except ValueError as error:
        flash(str(error), "danger")
        return redirect(url_for("backups.backup_detail", backup_id=backup.id))

    kick(run_recovery_job, job.id)
    flash("Restore started. Progress is shown on the Recoveries page.", "info")
    return redirect(url_for("backups.list_recoveries"))


@backups_bp.route("/recoveries")
@login_required
def list_recoveries():
    jobs = (
        RecoveryJob.query.filter_by(owner_id=current_user.id)
        .order_by(RecoveryJob.created_at.desc())
        .limit(100)
        .all()
    )
    return render_template(
        "backups/recoveries.html",
        jobs=jobs,
        any_active=any(job.is_active for job in jobs),
    )


@backups_bp.route("/recoveries/<int:job_id>/download")
@login_required
def download_restored(job_id):
    job = RecoveryJob.query.filter_by(id=job_id, owner_id=current_user.id).first_or_404()
    if job.status != "SUCCEEDED" or job.restore_mode != "SERVER_DOWNLOAD" or not job.restored_file:
        abort(404)

    # Defence in depth: the stored path must still live inside this user's restore folder.
    restored = Path(job.restored_file).resolve()
    user_root = (Path(current_app.config["RESTORE_DIR"]).resolve() / str(current_user.id))
    if not restored.is_relative_to(user_root) or not restored.is_file():
        abort(404)

    audit("RESTORED_FILE_DOWNLOADED", "recovery_job", job.id)
    db.session.commit()
    return send_file(restored, as_attachment=True, download_name=restored.name)
