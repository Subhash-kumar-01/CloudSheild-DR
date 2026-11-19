from app.extensions import db
from app.utils import utcnow


class AuditLog(db.Model):
    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    action = db.Column(db.String(120), nullable=False, index=True)
    resource_type = db.Column(db.String(80))
    resource_id = db.Column(db.String(80))
    ip_address = db.Column(db.String(64))
    details = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)


class SecurityEvent(db.Model):
    __tablename__ = "security_events"

    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    aws_connection_id = db.Column(
        db.Integer, db.ForeignKey("aws_connections.id", ondelete="CASCADE")
    )
    event_category = db.Column(db.String(80), nullable=False)
    severity = db.Column(db.String(20), nullable=False)
    source = db.Column(db.String(80), nullable=False)
    affected_resource = db.Column(db.String(300))
    actor = db.Column(db.String(300))
    details = db.Column(db.Text)
    investigation_status = db.Column(db.String(30), default="OPEN", nullable=False)
    recommended_response = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)
