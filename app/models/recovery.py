from app.extensions import db
from app.utils import utcnow


class RecoveryPoint(db.Model):
    __tablename__ = "recovery_points"

    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    backup_job_id = db.Column(
        db.Integer, db.ForeignKey("backup_jobs.id", ondelete="CASCADE"), nullable=False
    )
    aws_connection_id = db.Column(
        db.Integer, db.ForeignKey("aws_connections.id", ondelete="CASCADE"), nullable=False
    )
    recovery_point_time = db.Column(db.DateTime, nullable=False, index=True)
    source_resource = db.Column(db.String(300), nullable=False)
    # S3_FILE | S3_BUCKET_SYNC | EMERGENCY_PRESERVATION
    backup_type = db.Column(db.String(50), nullable=False)
    integrity_status = db.Column(db.String(30), default="UNKNOWN", nullable=False)
    # UNKNOWN | ACTIVE | EXPIRED
    retention_status = db.Column(db.String(30), default="UNKNOWN", nullable=False)
    version_id = db.Column(db.String(256))
    recovery_eligible = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    recovery_jobs = db.relationship(
        "RecoveryJob", backref="recovery_point", lazy=True, cascade="all"
    )

    @property
    def is_bucket_sync(self):
        return self.backup_type == "S3_BUCKET_SYNC"


class RecoveryJob(db.Model):
    __tablename__ = "recovery_jobs"

    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    incident_id = db.Column(db.Integer, db.ForeignKey("incidents.id", ondelete="CASCADE"))
    recovery_point_id = db.Column(
        db.Integer, db.ForeignKey("recovery_points.id", ondelete="CASCADE")
    )
    # SERVER_DOWNLOAD (single file -> downloadable) | S3_POINT_IN_TIME (bucket -> your S3)
    restore_mode = db.Column(db.String(30), default="SERVER_DOWNLOAD", nullable=False)
    destination = db.Column(db.String(500), nullable=False)
    restored_file = db.Column(db.String(500))
    # PENDING | RUNNING | SUCCEEDED | PARTIAL | FAILED
    status = db.Column(db.String(30), default="PENDING", nullable=False)
    verification_status = db.Column(db.String(30), default="PENDING", nullable=False)
    restored_objects = db.Column(db.Integer, default=0, nullable=False)
    failed_objects = db.Column(db.Integer, default=0, nullable=False)
    bytes_restored = db.Column(db.BigInteger, default=0, nullable=False)
    observed_recovery_seconds = db.Column(db.Integer)
    estimated_data_loss_seconds = db.Column(db.Integer)
    result_message = db.Column(db.Text)
    started_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    @property
    def is_active(self):
        return self.status in ("PENDING", "RUNNING")
