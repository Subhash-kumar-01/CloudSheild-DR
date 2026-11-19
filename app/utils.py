import re
from datetime import datetime, timezone
from pathlib import Path

from werkzeug.utils import secure_filename

BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")


def utcnow() -> datetime:
    """Naive UTC 'now'.

    All DateTime columns are timezone-naive (that is what the DB hands back),
    so every timestamp the app writes must be naive UTC too. Mixing aware and
    naive datetimes raises TypeError on subtraction.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def is_valid_bucket_name(name: str) -> bool:
    name = name or ""
    return bool(BUCKET_RE.fullmatch(name)) and ".." not in name


def safe_filename(filename: str) -> str:
    cleaned = secure_filename(Path(filename or "").name)
    return cleaned or "upload.bin"


def human_size(num) -> str:
    num = float(num or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{int(num)} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024


def fmt_dt(value) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC") if value else "-"


def is_safe_next_url(target: str) -> bool:
    return bool(target) and target.startswith("/") and not target.startswith("//") \
        and "\\" not in target


def audit(action, resource_type=None, resource_id=None, details=None, owner_id=None):
    """Append an audit log row (does not commit)."""
    from flask import has_request_context, request
    from flask_login import current_user

    from app.extensions import db
    from app.models import AuditLog

    if owner_id is None:
        owner_id = current_user.id
    db.session.add(
        AuditLog(
            owner_id=owner_id,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id is not None else None,
            ip_address=request.remote_addr if has_request_context() else None,
            details=details,
        )
    )
