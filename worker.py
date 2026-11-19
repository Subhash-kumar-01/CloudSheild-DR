"""Background worker: runs due scheduled backups and pending recovery jobs.

A plain polling loop (no scheduler library): state lives in the database, so a
restart never loses a schedule, and the claim logic makes duplicate runs
impossible even if several workers are started.
"""
import logging
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import inspect

from app import create_app
from app.extensions import db
from app.models import RecoveryJob
from app.services import schedule_service
from app.services.job_runner import run_recovery_job, run_scheduled_backup

log = logging.getLogger("cloudshield.worker")
stop_event = threading.Event()


def _handle_signal(signum, _frame):
    log.info("Signal %s received, shutting down after current work", signum)
    stop_event.set()


def wait_for_tables(app, timeout=120):
    """The web container creates the tables; wait for it."""
    deadline = time.time() + timeout
    while time.time() < deadline and not stop_event.is_set():
        with app.app_context():
            try:
                if inspect(db.engine).has_table("scheduled_backups"):
                    return True
            except Exception:  # noqa: BLE001 - DB may still be starting
                pass
        time.sleep(3)
    return False


def _in_context(app, fn, key, inflight, *args):
    try:
        with app.app_context():
            fn(*args)
    except Exception:  # noqa: BLE001
        log.exception("Job %s crashed", key)
    finally:
        inflight.discard(key)


def tick(app, executor, inflight):
    with app.app_context():
        schedule_service.mark_stale_jobs()
        schedule_service.expire_recovery_points()
        due = [("schedule", i) for i in schedule_service.due_schedule_ids()]
        due += [
            ("recovery", row[0])
            for row in RecoveryJob.query.filter_by(status="PENDING")
            .with_entities(RecoveryJob.id).all()
        ]
        db.session.remove()

    for kind, ident in due:
        key = (kind, ident)
        if key in inflight:
            continue
        inflight.add(key)
        fn = run_scheduled_backup if kind == "schedule" else run_recovery_job
        executor.submit(_in_context, app, fn, key, inflight, ident)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    app = create_app(init_db=False)
    if not app.config["SCHEDULER_ENABLED"]:
        log.warning("BACKGROUND_SCHEDULER_ENABLED=false: worker idle")
        stop_event.wait()
        return
    if not wait_for_tables(app):
        log.error("Database tables were not found; is the web service running?")
        return

    tick_seconds = app.config["SCHEDULER_TICK_SECONDS"]
    executor = ThreadPoolExecutor(max_workers=app.config["WORKER_CONCURRENCY"])
    inflight: set = set()
    log.info("Worker started (tick every %ss)", tick_seconds)

    while not stop_event.is_set():
        try:
            tick(app, executor, inflight)
        except Exception:  # noqa: BLE001 - keep the worker alive
            log.exception("Worker tick failed")
        stop_event.wait(tick_seconds)

    executor.shutdown(wait=True, cancel_futures=True)
    log.info("Worker stopped")


if __name__ == "__main__":
    main()
