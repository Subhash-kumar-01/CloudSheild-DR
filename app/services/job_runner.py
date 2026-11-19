"""Runs background work either in the worker process (normal) or in a daemon
thread kicked from a web request (so features work even if the worker is down).

Every job is claimed with an atomic UPDATE, so it never runs twice even when
both the worker and a web thread try."""
import threading

from flask import current_app

from app.extensions import db
from app.models import RecoveryJob
from app.services import schedule_service
from app.services.emergency_recovery_service import execute_recovery
from app.utils import utcnow


def claim_recovery_job(job_id: int) -> bool:
    updated = (
        RecoveryJob.query.filter(RecoveryJob.id == job_id, RecoveryJob.status == "PENDING")
        .update({"status": "RUNNING", "started_at": utcnow()}, synchronize_session=False)
    )
    db.session.commit()
    return updated == 1


def run_recovery_job(job_id: int):
    if claim_recovery_job(job_id):
        execute_recovery(job_id)


def run_scheduled_backup(schedule_id: int, force: bool = False):
    return schedule_service.run_schedule(schedule_id, force=force)


def _thread_main(app, fn, args, kwargs):
    with app.app_context():
        try:
            fn(*args, **kwargs)
        except Exception:  # noqa: BLE001
            app.logger.exception("Background task %s crashed", getattr(fn, "__name__", fn))
        finally:
            db.session.remove()


def kick(fn, *args, **kwargs):
    """Start fn(*args) in a daemon thread inside its own app context."""
    app = current_app._get_current_object()
    thread = threading.Thread(
        target=_thread_main, args=(app, fn, args, kwargs), daemon=True, name=f"job-{fn.__name__}"
    )
    thread.start()
    return thread
