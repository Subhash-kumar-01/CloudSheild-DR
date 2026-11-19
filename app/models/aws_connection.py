import re
import secrets
from fnmatch import fnmatchcase

from app.extensions import db
from app.utils import utcnow

ACCOUNT_ID_RE = re.compile(r"^\d{12}$")
ROLE_ARN_RE = re.compile(r"^arn:aws:iam::(\d{12}):role/[\w+=,.@/-]{1,512}$")


class AwsConnection(db.Model):
    __tablename__ = "aws_connections"
    __table_args__ = (
        db.UniqueConstraint("owner_id", "aws_account_id", name="uq_connection_owner_account"),
    )

    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    display_name = db.Column(db.String(120), nullable=False)
    aws_account_id = db.Column(db.String(12), nullable=False, index=True)
    region = db.Column(db.String(32), nullable=False)
    role_arn = db.Column(db.String(600), nullable=False)
    external_id = db.Column(db.String(128), unique=True, nullable=False)
    status = db.Column(db.String(20), default="PENDING", nullable=False)
    status_message = db.Column(db.Text)
    backup_bucket = db.Column(db.String(255))
    # Which source buckets the customer's IAM role may read (CloudFormation
    # parameter SourceBucketScope): "*", "my-bucket" or a pattern like "prod-*".
    source_scope = db.Column(db.String(255), default="*", nullable=False)
    # Must equal the RetentionDays used when the CloudFormation stack is deployed.
    retention_days = db.Column(db.Integer, default=30, nullable=False)
    last_verified_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    # Deleting a connection removes CloudShield's records only. Data in the
    # customer's S3 buckets is never touched.
    backups = db.relationship("BackupJob", backref="aws_connection", lazy=True, cascade="all")
    schedules = db.relationship(
        "ScheduledBackup", backref="aws_connection", lazy=True, cascade="all"
    )
    recovery_points = db.relationship(
        "RecoveryPoint", backref="aws_connection", lazy=True, cascade="all"
    )
    incidents = db.relationship("Incident", backref="aws_connection", lazy=True, cascade="all")
    security_events = db.relationship(
        "SecurityEvent", backref="aws_connection", lazy=True, cascade="all"
    )

    @staticmethod
    def generate_external_id():
        return secrets.token_urlsafe(32)

    @staticmethod
    def is_valid_account_id(value):
        return bool(ACCOUNT_ID_RE.fullmatch(value or ""))

    @staticmethod
    def is_valid_role_arn(value):
        return bool(ROLE_ARN_RE.fullmatch(value or ""))

    @staticmethod
    def account_from_role_arn(value):
        match = ROLE_ARN_RE.fullmatch(value or "")
        return match.group(1) if match else None

    @staticmethod
    def default_role_arn(account_id: str, role_name: str) -> str:
        return f"arn:aws:iam::{account_id}:role/{role_name}"

    @staticmethod
    def suggest_bucket_name(account_id: str) -> str:
        return f"cloudshield-dr-{account_id}-{secrets.token_hex(3)}"

    def scope_allows(self, bucket: str) -> bool:
        """True if `bucket` matches the IAM scope the customer granted."""
        return fnmatchcase(bucket, self.source_scope or "*")

    @property
    def is_connected(self):
        return self.status == "CONNECTED"
