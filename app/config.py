import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Load .env BEFORE any os.getenv below. (Previously load_dotenv() ran after this
# module was imported, so values from .env were silently ignored.)
load_dotenv(BASE_DIR / ".env")

INSECURE_SECRETS = {
    "",
    "dev-only-change-me",
    "replace-with-a-long-random-secret",
}


def _bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-me")
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", "sqlite:///cloudshield.db")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True, "pool_recycle": 1800}

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE")
    PERMANENT_SESSION_LIFETIME = 60 * 60 * 8

    AWS_DEFAULT_REGION = os.getenv("AWS_DEFAULT_REGION", "ap-south-1")
    CLOUDSHIELD_AWS_ACCOUNT_ID = os.getenv("CLOUDSHIELD_AWS_ACCOUNT_ID", "")
    CLOUDSHIELD_BACKUP_ROLE_NAME = os.getenv(
        "CLOUDSHIELD_BACKUP_ROLE_NAME", "CloudShieldDRBackupRole"
    )
    # STS session length. Role-chaining (app running on an IAM role) caps this at 1h;
    # sessions are auto-refreshed, so long backups still work.
    AWS_ASSUME_ROLE_SECONDS = int(os.getenv("AWS_ASSUME_ROLE_SECONDS", "3600"))

    UPLOAD_DIR = Path(os.getenv("BACKUP_UPLOAD_DIR", BASE_DIR / "instance/uploads"))
    RESTORE_DIR = Path(os.getenv("RESTORE_DOWNLOAD_DIR", BASE_DIR / "instance/restores"))

    MAX_CONTENT_LENGTH = int(os.getenv("MAX_BACKUP_SIZE_MB", "512")) * 1024 * 1024
    ALLOWED_EXTENSIONS = {
        ".zip", ".tar", ".gz", ".tgz", ".7z", ".bz2", ".xz",
        ".sql", ".dump", ".bak", ".sqlite", ".db",
        ".csv", ".json", ".xml", ".yaml", ".yml", ".txt", ".log", ".parquet",
        ".pdf", ".docx", ".xlsx", ".pptx",
        ".png", ".jpg", ".jpeg", ".gif", ".webp",
    }

    # ---- cost controls -------------------------------------------------
    # INTELLIGENT_TIERING has no minimum storage duration and no retrieval fee,
    # which suits versioned backups whose old versions expire by lifecycle.
    BACKUP_STORAGE_CLASS = os.getenv("BACKUP_STORAGE_CLASS", "INTELLIGENT_TIERING")
    ALLOWED_STORAGE_CLASSES = ("INTELLIGENT_TIERING", "STANDARD_IA", "GLACIER_IR", "STANDARD")
    MIN_SCHEDULE_MINUTES = 60          # each run costs LIST requests; hourly is the floor
    RESTORE_PREFIX = "cloudshield-restore"  # the ONLY prefix CloudShield may write to in source buckets
    SYNC_WORKERS = int(os.getenv("SYNC_WORKERS", "8"))

    # ---- background worker ---------------------------------------------
    SCHEDULER_ENABLED = _bool("BACKGROUND_SCHEDULER_ENABLED", "true")
    SCHEDULER_TICK_SECONDS = int(os.getenv("SCHEDULER_TICK_SECONDS", "30"))
    WORKER_CONCURRENCY = int(os.getenv("WORKER_CONCURRENCY", "2"))
    STALE_JOB_HOURS = int(os.getenv("STALE_JOB_HOURS", "6"))

    AUTO_CREATE_TABLES = _bool("AUTO_CREATE_TABLES", "true")
    LOGIN_MAX_FAILURES = 5
    LOGIN_LOCK_MINUTES = 15


class DevelopmentConfig(Config):
    DEBUG = True


class TestingConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {}
    WTF_CSRF_ENABLED = False
    SCHEDULER_ENABLED = False
    SECRET_KEY = "testing-secret-key-not-for-production"


class ProductionConfig(Config):
    DEBUG = False


config_by_name = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
}
