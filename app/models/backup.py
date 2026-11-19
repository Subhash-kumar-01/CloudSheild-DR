import uuid

from app.extensions import db
from app.utils import utcnow


class BackupJob(db.Model):
    __tablename__ = "backup_jobs"

    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    aws_connection_id = db.Column(
        db.Integer, db.ForeignKey("aws_connections.id", ondelete="CASCADE"), nullable=False
    )
    schedule_id = db.Column(
        db.Integer, db.ForeignKey("scheduled_backups.id", ondelete="SET NULL"), index=True
    )
    backup_name = db.Column(db.String(180), nullable=False)
    # FILE_UPLOAD | S3_BUCKET_SYNC | EMERGENCY_PRESERVATION | SCHEDULED (legacy)
    source_type = db.Column(db.String(30), nullable=False)
    source_description = db.Column(db.Text)
    source_bucket = db.Column(db.String(255))
    source_prefix = db.Column(db.String(1024))
    destination_bucket = db.Column(db.String(255), nullable=False)
    object_key = db.Column(db.String(1024))
    version_id = db.Column(db.String(256))
    # PENDING | RUNNING | SUCCEEDED | PARTIAL | FAILED
    status = db.Column(db.String(30), default="PENDING", nullable=False)
    size_bytes = db.Column(db.BigInteger, default=0, nullable=False)
    objects_scanned = db.Column(db.Integer, default=0, nullable=False)
    objects_copied = db.Column(db.Integer, default=0, nullable=False)
    objects_failed = db.Column(db.Integer, default=0, nullable=False)
    objects_missing_at_source = db.Column(db.Integer, default=0, nullable=False)
    sha256_checksum = db.Column(db.String(64))
    s3_checksum = db.Column(db.String(256))
    error_message = db.Column(db.Text)
    started_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)

    recovery_points = db.relationship(
        "RecoveryPoint", backref="backup_job", lazy=True, cascade="all"
    )

    @property
    def is_bucket_sync(self):
        return self.source_type == "S3_BUCKET_SYNC"

    @property
    def duration_seconds(self):
        if self.started_at and self.completed_at:
            return int((self.completed_at - self.started_at).total_seconds())
        return None


class ScheduledBackup(db.Model):
    __tablename__ = "scheduled_backups"

    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    aws_connection_id = db.Column(
        db.Integer, db.ForeignKey("aws_connections.id", ondelete="CASCADE"), nullable=False
    )
    name = db.Column(db.String(180), nullable=False)
    source_type = db.Column(db.String(30), default="S3_BUCKET", nullable=False)
    source_bucket = db.Column(db.String(255))
    source_prefix = db.Column(db.String(1024), default="", nullable=False)
    destination_bucket = db.Column(db.String(255), nullable=False)
    storage_class = db.Column(db.String(30), default="INTELLIGENT_TIERING", nullable=False)
    frequency = db.Column(db.String(20), nullable=False)
    interval_minutes = db.Column(db.Integer, default=60, nullable=False)
    timezone_name = db.Column(db.String(64), default="Asia/Kolkata", nullable=False)
    retention_days = db.Column(db.Integer, default=30, nullable=False)
    enabled = db.Column(db.Boolean, default=True, nullable=False)
    next_run_at = db.Column(db.DateTime, index=True)
    running_since = db.Column(db.DateTime)
    last_run_at = db.Column(db.DateTime)
    last_status = db.Column(db.String(30))
    last_message = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    # Deleting a schedule keeps its history (BackupJob.schedule_id becomes NULL).
    jobs = db.relationship("BackupJob", backref="schedule", lazy=True)

    @staticmethod
    def new_idempotency_token():
        return str(uuid.uuid4())

    @property
    def is_running(self):
        return self.running_since is not None

    @property
    def source_display(self):
        return f"s3://{self.source_bucket}/{self.source_prefix or ''}"
