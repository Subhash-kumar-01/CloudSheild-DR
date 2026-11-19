from app.extensions import db
from app.utils import utcnow


class Incident(db.Model):
    __tablename__ = "incidents"

    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    aws_connection_id = db.Column(
        db.Integer, db.ForeignKey("aws_connections.id", ondelete="CASCADE"), nullable=False
    )
    title = db.Column(db.String(200), nullable=False)
    incident_type = db.Column(db.String(50), nullable=False)
    affected_resource = db.Column(db.String(300), nullable=False)
    severity = db.Column(db.String(20), nullable=False)
    # OPEN | CONTAINED | DATA_PRESERVED | RECOVERY_IN_PROGRESS | RECOVERY_FAILED | RECOVERED | CLOSED
    status = db.Column(db.String(30), default="OPEN", nullable=False)
    suspected_cause = db.Column(db.Text)
    description = db.Column(db.Text)
    reported_data_loss_percent = db.Column(db.Float, nullable=False)
    reported_remaining_percent = db.Column(db.Float, nullable=False)
    discovered_at = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    recovery_jobs = db.relationship(
        "RecoveryJob", backref="incident", lazy=True, cascade="all"
    )

    ACTIVE_STATUSES = ("OPEN", "CONTAINED", "DATA_PRESERVED", "RECOVERY_IN_PROGRESS", "RECOVERY_FAILED")

    @property
    def aws_connection_ref(self):  # backwards-compatible alias
        return self.aws_connection

    @property
    def is_active(self):
        return self.status in self.ACTIVE_STATUSES
