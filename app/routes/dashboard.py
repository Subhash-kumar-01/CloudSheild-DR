from flask import Blueprint, render_template
from flask_login import current_user, login_required
from sqlalchemy import func

from app.extensions import db
from app.models import (
    AwsConnection, BackupJob, Incident, RecoveryJob, ScheduledBackup, SecurityEvent,
)

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/")
@login_required
def index():
    owner_id = current_user.id

    connected_accounts = AwsConnection.query.filter_by(
        owner_id=owner_id, status="CONNECTED"
    ).count()
    total_accounts = AwsConnection.query.filter_by(owner_id=owner_id).count()

    def count_backups(status=None):
        query = BackupJob.query.filter_by(owner_id=owner_id)
        return (query.filter_by(status=status) if status else query).count()

    total_backups = count_backups()
    successful_backups = count_backups("SUCCEEDED")
    failed_backups = count_backups("FAILED") + count_backups("PARTIAL")

    total_bytes = (
        db.session.query(func.coalesce(func.sum(BackupJob.size_bytes), 0))
        .filter(BackupJob.owner_id == owner_id)
        .scalar()
    )

    open_incidents = Incident.query.filter(
        Incident.owner_id == owner_id, Incident.status.in_(Incident.ACTIVE_STATUSES)
    ).count()

    active_schedules = ScheduledBackup.query.filter_by(owner_id=owner_id, enabled=True).count()
    last_success = (
        BackupJob.query.filter_by(owner_id=owner_id, status="SUCCEEDED")
        .order_by(BackupJob.completed_at.desc())
        .first()
    )
    recent_backups = (
        BackupJob.query.filter_by(owner_id=owner_id)
        .order_by(BackupJob.created_at.desc())
        .limit(5)
        .all()
    )
    recent_recoveries = (
        RecoveryJob.query.filter_by(owner_id=owner_id)
        .order_by(RecoveryJob.created_at.desc())
        .limit(5)
        .all()
    )
    security_events = (
        SecurityEvent.query.filter_by(owner_id=owner_id)
        .order_by(SecurityEvent.created_at.desc())
        .limit(5)
        .all()
    )

    return render_template(
        "dashboard/index.html",
        connected_accounts=connected_accounts,
        total_accounts=total_accounts,
        total_backups=total_backups,
        successful_backups=successful_backups,
        failed_backups=failed_backups,
        total_bytes=total_bytes,
        open_incidents=open_incidents,
        active_schedules=active_schedules,
        last_success=last_success,
        recent_backups=recent_backups,
        recent_recoveries=recent_recoveries,
        security_events=security_events,
    )
