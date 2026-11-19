"""Incident handling and recovery execution.

Recovery never overwrites live data:
  * bucket recovery points are restored (server-side copy, exact versions) into
    s3://<source bucket>/cloudshield-restore/job-<id>/ in the customer's account;
  * single-file recovery points are downloaded, checksum-verified, and offered
    as a download.
"""
from flask import current_app

from app.extensions import db
from app.models import Incident, RecoveryJob, RecoveryPoint, SecurityEvent
from app.services import s3_sync
from app.services.aws_connection_service import build_session
from app.services.s3_backup_service import resolve_restore_dir, restore_file_to_server
from app.utils import audit, utcnow

INCIDENT_PLAYBOOK = {
    "RANSOMWARE": (
        "Isolate affected systems, rotate AWS credentials/keys used by them, and review "
        "CloudTrail. Restore only from a recovery point BEFORE the compromise."
    ),
    "ACCIDENTAL_DELETION": "Choose the latest recovery point from before the deletion.",
    "OBJECT_OVERWRITE": "Choose the latest recovery point from before the overwrite.",
}


def create_incident(owner_id: int, connection_id: int, form_data: dict) -> Incident:
    loss = float(form_data["reported_data_loss_percent"])
    discovered_at = form_data.get("discovered_at") or utcnow()

    incident = Incident(
        owner_id=owner_id,
        aws_connection_id=connection_id,
        title=form_data["title"].strip(),
        incident_type=form_data["incident_type"],
        affected_resource=form_data["affected_resource"].strip(),
        severity=form_data["severity"],
        suspected_cause=form_data.get("suspected_cause") or None,
        description=form_data.get("description") or None,
        reported_data_loss_percent=loss,
        reported_remaining_percent=max(0.0, 100.0 - loss),
        discovered_at=discovered_at,
        status="OPEN",
    )
    db.session.add(incident)
    db.session.flush()

    audit("INCIDENT_CREATED", "incident", incident.id, owner_id=owner_id)

    if incident.incident_type == "RANSOMWARE" or incident.severity == "CRITICAL":
        db.session.add(
            SecurityEvent(
                owner_id=owner_id,
                aws_connection_id=connection_id,
                event_category="INCIDENT_REPORTED",
                severity=incident.severity,
                source="USER_REPORT",
                affected_resource=incident.affected_resource,
                details=f"Incident #{incident.id}: {incident.title}",
                recommended_response=INCIDENT_PLAYBOOK.get(
                    incident.incident_type,
                    "Preserve remaining data, then restore from a verified recovery point.",
                ),
            )
        )
    db.session.commit()
    return incident


def get_eligible_recovery_points(owner_id: int, connection_id: int):
    return (
        RecoveryPoint.query.filter_by(
            owner_id=owner_id,
            aws_connection_id=connection_id,
            recovery_eligible=True,
            integrity_status="VERIFIED",
        )
        .order_by(RecoveryPoint.recovery_point_time.desc())
        .limit(200)
        .all()
    )


def start_recovery(owner_id: int, incident_id, recovery_point_id: int, destination: str = ""):
    """Create a PENDING recovery job (executed by the worker or an in-process thread).

    Raises ValueError with a user-presentable message on invalid input."""
    incident = None
    if incident_id is not None:
        incident = Incident.query.filter_by(id=incident_id, owner_id=owner_id).first_or_404()
    point = RecoveryPoint.query.filter_by(id=recovery_point_id, owner_id=owner_id).first_or_404()

    if incident and point.aws_connection_id != incident.aws_connection_id:
        raise ValueError("That recovery point belongs to a different AWS connection.")
    if not point.recovery_eligible:
        raise ValueError("This recovery point is not eligible for recovery (expired or unverified).")
    if not point.aws_connection.is_connected:
        raise ValueError("The AWS connection is not verified. Verify it first.")

    destination = (destination or "").strip()
    mode = "S3_POINT_IN_TIME" if point.is_bucket_sync else "SERVER_DOWNLOAD"
    if mode == "SERVER_DOWNLOAD":
        if not destination:
            destination = "restored"
        if resolve_restore_dir(owner_id, destination, 0) is None:
            raise ValueError("Use a simple folder name (no absolute paths or '..').")

    job = RecoveryJob(
        owner_id=owner_id,
        incident_id=incident.id if incident else None,
        recovery_point_id=point.id,
        restore_mode=mode,
        destination=destination or "pending",
        status="PENDING",
    )
    db.session.add(job)
    db.session.flush()

    if mode == "S3_POINT_IN_TIME":
        job.destination = (
            f"s3://{point.backup_job.source_bucket}/"
            f"{current_app.config['RESTORE_PREFIX']}/job-{job.id}/"
        )
    if incident:
        incident.status = "RECOVERY_IN_PROGRESS"
    audit("RECOVERY_REQUESTED", "recovery_job", job.id, owner_id=owner_id)
    db.session.commit()
    return job


def _finish(job: RecoveryJob, status: str, message: str, verification: str):
    job.status = status
    job.verification_status = verification
    job.result_message = message
    job.completed_at = utcnow()
    if job.started_at:
        job.observed_recovery_seconds = int((job.completed_at - job.started_at).total_seconds())
    if job.incident and job.recovery_point:
        gap = (job.incident.discovered_at - job.recovery_point.recovery_point_time).total_seconds()
        job.estimated_data_loss_seconds = max(0, int(gap))
    if job.incident:
        job.incident.status = "RECOVERED" if status == "SUCCEEDED" else "RECOVERY_FAILED"
    db.session.commit()


def _restore_bucket_point(job, point, backup, connection):
    from boto3.s3.transfer import TransferConfig

    dest_bucket = backup.source_bucket
    dest_prefix = f"{current_app.config['RESTORE_PREFIX']}/job-{job.id}/"

    if not connection.scope_allows(dest_bucket):
        return _finish(
            job, "FAILED",
            f"Bucket '{dest_bucket}' is outside the source scope granted to CloudShield.",
            "FAILED",
        )

    s3 = build_session(connection).client("s3")
    result = s3_sync.run_restore_as_of(
        s3,
        backup_bucket=backup.destination_bucket,
        source_bucket=backup.source_bucket,
        source_prefix=backup.source_prefix or "",
        as_of=point.recovery_point_time,
        dest_bucket=dest_bucket,
        dest_prefix=dest_prefix,
        workers=current_app.config["SYNC_WORKERS"],
        transfer_config=TransferConfig(use_threads=False),
    )
    job.restored_objects = result.restored
    job.failed_objects = result.failed
    job.bytes_restored = result.bytes_restored
    where = f"s3://{dest_bucket}/{dest_prefix}"

    if result.selected == 0:
        return _finish(
            job, "FAILED",
            "No backed-up objects were found for that point in time. The retention period "
            "may have expired, or nothing had been backed up yet.",
            "FAILED",
        )
    if result.failed == 0 and result.restored == result.selected:
        return _finish(
            job, "SUCCEEDED",
            f"Restored {result.restored} object(s) to {where}. Each copy was confirmed by S3. "
            "Review the files, then copy them back where you need them.",
            "VERIFIED",
        )
    detail = "; ".join(result.errors[:5])
    return _finish(
        job, "PARTIAL" if result.restored else "FAILED",
        f"Restored {result.restored} of {result.selected} object(s) to {where}. {detail}",
        "FAILED",
    )


def execute_recovery(job_id: int):
    """Run a recovery job that has already been claimed (status RUNNING)."""
    job = db.session.get(RecoveryJob, job_id)
    if job is None:
        return
    try:
        point = job.recovery_point
        backup = point.backup_job
        connection = point.aws_connection
        if point.is_bucket_sync:
            _restore_bucket_point(job, point, backup, connection)
        else:
            restore_file_to_server(connection, backup, job)
            if job.incident:
                job.incident.status = "RECOVERED" if job.status == "SUCCEEDED" else "RECOVERY_FAILED"
            job.completed_at = job.completed_at or utcnow()
            if job.started_at:
                job.observed_recovery_seconds = int(
                    (job.completed_at - job.started_at).total_seconds()
                )
            if job.incident and job.recovery_point:
                gap = (job.incident.discovered_at - job.recovery_point.recovery_point_time)
                job.estimated_data_loss_seconds = max(0, int(gap.total_seconds()))
            db.session.commit()
    except Exception:  # noqa: BLE001 - never leave a job stuck in RUNNING
        db.session.rollback()
        current_app.logger.exception("Recovery job %s failed", job_id)
        job = db.session.get(RecoveryJob, job_id)
        if job is not None:
            _finish(job, "FAILED", "Recovery failed unexpectedly. Check the server log.", "FAILED")
