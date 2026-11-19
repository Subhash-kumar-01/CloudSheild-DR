"""Scheduled bucket backups: claiming, execution, retention and housekeeping."""
from datetime import timedelta

from boto3.s3.transfer import TransferConfig
from flask import current_app
from sqlalchemy import or_

from app.extensions import db
from app.models import (
    AwsConnection, BackupJob, RecoveryJob, RecoveryPoint, ScheduledBackup, SecurityEvent,
)
from app.services import s3_sync
from app.services.aws_connection_service import build_session
from app.utils import utcnow

FREQUENCIES = {"HOURLY": 60, "DAILY": 1440, "WEEKLY": 10080}


def _stale_cutoff():
    return utcnow() - timedelta(hours=current_app.config["STALE_JOB_HOURS"])


def claim_schedule(schedule_id: int) -> bool:
    """Atomically mark a schedule as running. Safe across any number of
    processes/threads: only one UPDATE can match."""
    updated = (
        ScheduledBackup.query.filter(
            ScheduledBackup.id == schedule_id,
            or_(
                ScheduledBackup.running_since.is_(None),
                ScheduledBackup.running_since < _stale_cutoff(),
            ),
        ).update({"running_since": utcnow()}, synchronize_session=False)
    )
    db.session.commit()
    return updated == 1


def due_schedule_ids() -> list[int]:
    now = utcnow()
    rows = (
        ScheduledBackup.query.filter(
            ScheduledBackup.enabled.is_(True),
            or_(ScheduledBackup.next_run_at.is_(None), ScheduledBackup.next_run_at <= now),
            or_(
                ScheduledBackup.running_since.is_(None),
                ScheduledBackup.running_since < _stale_cutoff(),
            ),
        )
        .with_entities(ScheduledBackup.id)
        .all()
    )
    return [r[0] for r in rows]


def _event_recently_raised(owner_id, category, resource) -> bool:
    since = utcnow() - timedelta(hours=24)
    return (
        SecurityEvent.query.filter(
            SecurityEvent.owner_id == owner_id,
            SecurityEvent.event_category == category,
            SecurityEvent.affected_resource == resource,
            SecurityEvent.created_at >= since,
        ).first()
        is not None
    )


def _summary(result: s3_sync.SyncResult) -> str:
    parts = [
        f"{result.scanned} object(s) scanned",
        f"{result.copied} copied ({result.new} new, {result.changed} changed)",
        f"{result.unchanged} unchanged",
    ]
    if result.archived_skipped:
        parts.append(f"{result.archived_skipped} archived (Glacier) object(s) skipped")
    if result.missing_at_source:
        parts.append(f"{result.missing_at_source} no longer in source (kept in backup)")
    return ", ".join(parts)


def run_schedule(schedule_id: int, force: bool = False):
    """Execute one scheduled backup. Returns the BackupJob, or None if the
    schedule was already running / disabled."""
    if not claim_schedule(schedule_id):
        return None

    schedule = db.session.get(ScheduledBackup, schedule_id)
    db.session.refresh(schedule)
    interval = timedelta(minutes=schedule.interval_minutes)

    def release(status, message):
        schedule.running_since = None
        schedule.last_run_at = utcnow()
        schedule.last_status = status
        schedule.last_message = (message or "")[:1000]
        schedule.next_run_at = utcnow() + interval
        db.session.commit()

    if not schedule.enabled and not force:
        schedule.running_since = None
        db.session.commit()
        return None

    connection = schedule.aws_connection
    backup = BackupJob(
        owner_id=schedule.owner_id,
        aws_connection_id=schedule.aws_connection_id,
        schedule_id=schedule.id,
        backup_name=f"Scheduled backup - {schedule.name}",
        source_type="S3_BUCKET_SYNC",
        source_bucket=schedule.source_bucket,
        source_prefix=schedule.source_prefix,
        source_description=schedule.source_display,
        destination_bucket=schedule.destination_bucket,
        status="RUNNING",
        started_at=utcnow(),
    )
    db.session.add(backup)
    db.session.commit()

    try:
        if not connection.is_connected:
            raise ValueError("The AWS connection is not verified. Verify it, then run again.")
        if not connection.backup_bucket:
            raise ValueError("No backup bucket is configured for this AWS connection.")

        s3 = build_session(connection).client("s3")
        result = s3_sync.run_sync(
            s3,
            source_bucket=schedule.source_bucket,
            source_prefix=schedule.source_prefix,
            backup_bucket=schedule.destination_bucket,
            storage_class=schedule.storage_class,
            workers=current_app.config["SYNC_WORKERS"],
            transfer_config=TransferConfig(use_threads=False),
        )

        backup.completed_at = utcnow()
        backup.size_bytes = result.bytes_copied
        backup.objects_scanned = result.scanned
        backup.objects_copied = result.copied
        backup.objects_failed = result.failed
        backup.objects_missing_at_source = result.missing_at_source
        backup.source_description = f"{schedule.source_display}: {_summary(result)}"

        if result.failed == 0:
            backup.status = "SUCCEEDED"
        elif result.copied > 0:
            backup.status = "PARTIAL"
        else:
            backup.status = "FAILED"
        if result.errors:
            backup.error_message = "; ".join(result.errors)[:2000]

        # A recovery point is only worth recording when the state changed (or it is
        # the first one): an unchanged run is already covered by the previous point.
        has_previous = (
            RecoveryPoint.query.join(BackupJob, RecoveryPoint.backup_job_id == BackupJob.id)
            .filter(BackupJob.schedule_id == schedule.id, RecoveryPoint.recovery_eligible.is_(True))
            .first()
            is not None
        )
        if backup.status == "SUCCEEDED" and (result.copied > 0 or not has_previous):
            db.session.add(
                RecoveryPoint(
                    owner_id=schedule.owner_id,
                    backup_job_id=backup.id,
                    aws_connection_id=schedule.aws_connection_id,
                    recovery_point_time=backup.completed_at,
                    source_resource=schedule.source_display,
                    backup_type="S3_BUCKET_SYNC",
                    integrity_status="VERIFIED",
                    retention_status="ACTIVE",
                    recovery_eligible=True,
                )
            )

        for finding in s3_sync.detect_anomalies(result):
            if not _event_recently_raised(
                schedule.owner_id, finding["category"], schedule.source_display
            ):
                db.session.add(
                    SecurityEvent(
                        owner_id=schedule.owner_id,
                        aws_connection_id=schedule.aws_connection_id,
                        event_category=finding["category"],
                        severity=finding["severity"],
                        source="SYNC_ANALYSIS",
                        affected_resource=schedule.source_display,
                        details=finding["details"],
                        recommended_response=finding["response"],
                    )
                )

        db.session.commit()
        release(backup.status, backup.source_description)
        return backup

    except Exception as error:  # noqa: BLE001 - always release the schedule
        db.session.rollback()
        current_app.logger.exception("Scheduled backup %s failed", schedule_id)
        backup = db.session.get(BackupJob, backup.id)
        schedule = db.session.get(ScheduledBackup, schedule_id)
        message = _safe_message(error)
        backup.status = "FAILED"
        backup.error_message = message
        backup.completed_at = utcnow()
        schedule.running_since = None
        schedule.last_run_at = utcnow()
        schedule.last_status = "FAILED"
        schedule.last_message = message[:1000]
        schedule.next_run_at = utcnow() + interval
        db.session.commit()
        return backup


def _safe_message(error: Exception) -> str:
    if isinstance(error, ValueError):
        return str(error)[:500]
    code = getattr(error, "response", {}).get("Error", {}).get("Code") if hasattr(error, "response") else None
    if code == "AccessDenied":
        return "AWS denied access. Check the IAM role permissions and the source bucket scope."
    if code in ("NoSuchBucket", "404"):
        return "A bucket was not found. Check the source and backup bucket names."
    if code:
        return f"AWS operation failed: {code}"
    return "Backup failed unexpectedly. Check the server log."


def expire_recovery_points() -> int:
    """Mark bucket recovery points older than the connection's retention as
    expired. The customer's S3 lifecycle rule deletes the matching old versions,
    so restoring from them would no longer be complete."""
    now = utcnow()
    rows = (
        db.session.query(RecoveryPoint, AwsConnection)
        .join(AwsConnection, RecoveryPoint.aws_connection_id == AwsConnection.id)
        .filter(
            RecoveryPoint.backup_type == "S3_BUCKET_SYNC",
            RecoveryPoint.recovery_eligible.is_(True),
        )
        .all()
    )
    expired = 0
    for point, connection in rows:
        if point.recovery_point_time < now - timedelta(days=connection.retention_days):
            point.recovery_eligible = False
            point.retention_status = "EXPIRED"
            expired += 1
    if expired:
        db.session.commit()
    return expired


def mark_stale_jobs() -> int:
    """Fail jobs left RUNNING by a crashed/restarted process."""
    cutoff = _stale_cutoff()
    count = 0
    for model in (BackupJob, RecoveryJob):
        stuck = model.query.filter(model.status == "RUNNING", model.started_at < cutoff).all()
        for job in stuck:
            job.status = "FAILED"
            job.completed_at = utcnow()
            message = "Interrupted (the worker restarted or timed out)."
            if model is BackupJob:
                job.error_message = message
            else:
                job.result_message = message
            count += 1
    if count:
        db.session.commit()
    return count
