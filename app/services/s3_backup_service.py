"""Single-file backups (upload -> S3), emergency preservation, version listing
and single-file restore. Bucket-level backups live in schedule_service /
s3_sync; bucket-level restores in emergency_recovery_service."""
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath

from botocore.exceptions import ClientError
from flask import current_app

from app.extensions import db
from app.models import BackupJob, RecoveryPoint
from app.services.aws_connection_service import build_session
from app.services.integrity_service import calculate_sha256, verify_file_checksum
from app.utils import safe_filename, utcnow


def safe_aws_error(error: ClientError) -> str:
    code = error.response.get("Error", {}).get("Code", "UnknownError")
    messages = {
        "AccessDenied": "AWS denied the backup operation. Check the IAM role S3 permissions.",
        "NoSuchBucket": "The configured backup bucket does not exist.",
        "InvalidAccessKeyId": "AWS rejected the temporary credentials.",
        "ExpiredToken": "The temporary AWS session expired.",
    }
    return messages.get(code, f"AWS operation failed: {code}")


def current_upload_dir() -> Path:
    return Path(current_app.config["UPLOAD_DIR"])


def _upload_extra_args():
    return {
        "ServerSideEncryption": "AES256",
        "ChecksumAlgorithm": "SHA256",
        "StorageClass": current_app.config["BACKUP_STORAGE_CLASS"],
    }


def create_backup_from_upload(
    connection, uploaded_file, backup_name: str, description: str = ""
) -> BackupJob:
    original_name = safe_filename(uploaded_file.filename)

    backup = BackupJob(
        owner_id=connection.owner_id,
        aws_connection_id=connection.id,
        backup_name=backup_name,
        source_type="FILE_UPLOAD",
        source_description=description,
        destination_bucket=connection.backup_bucket or "",
        status="RUNNING",
        started_at=utcnow(),
    )
    db.session.add(backup)
    db.session.commit()

    local_path = None
    try:
        if not connection.backup_bucket:
            raise ValueError("No backup bucket is configured for this AWS connection.")

        upload_dir = current_upload_dir()
        upload_dir.mkdir(parents=True, exist_ok=True)
        local_path = upload_dir / f"{backup.id}_{uuid.uuid4().hex}_{original_name}"
        uploaded_file.save(local_path)

        checksum = calculate_sha256(local_path)
        size = local_path.stat().st_size
        object_key = f"backups/{connection.owner_id}/{backup.id}/{original_name}"

        s3_client = build_session(connection).client("s3")
        s3_client.upload_file(
            Filename=str(local_path),
            Bucket=connection.backup_bucket,
            Key=object_key,
            ExtraArgs=_upload_extra_args(),
        )
        head = s3_client.head_object(
            Bucket=connection.backup_bucket, Key=object_key, ChecksumMode="ENABLED"
        )
        if head.get("ContentLength") != size:
            raise ValueError("Uploaded object size does not match the local file.")

        backup.object_key = object_key
        backup.version_id = head.get("VersionId")
        backup.size_bytes = size
        backup.sha256_checksum = checksum
        backup.s3_checksum = head.get("ChecksumSHA256")
        backup.status = "SUCCEEDED"
        backup.completed_at = utcnow()

        db.session.add(
            RecoveryPoint(
                owner_id=connection.owner_id,
                backup_job_id=backup.id,
                aws_connection_id=connection.id,
                recovery_point_time=backup.completed_at,
                source_resource=original_name,
                backup_type="S3_FILE",
                integrity_status="VERIFIED",
                retention_status="ACTIVE",
                version_id=backup.version_id,
                recovery_eligible=True,
            )
        )
        db.session.commit()
        return backup

    except ClientError as error:
        db.session.rollback()
        backup.status = "FAILED"
        backup.error_message = safe_aws_error(error)
        backup.completed_at = utcnow()
        db.session.commit()
        raise
    except Exception as error:
        db.session.rollback()
        backup.status = "FAILED"
        backup.error_message = str(error)[:1000]
        backup.completed_at = utcnow()
        db.session.commit()
        raise
    finally:
        if local_path is not None:
            local_path.unlink(missing_ok=True)


def list_object_versions(connection, bucket: str, object_key: str):
    s3_client = build_session(connection).client("s3")
    response = s3_client.list_object_versions(Bucket=bucket, Prefix=object_key)
    return [
        {
            "version_id": v["VersionId"],
            "last_modified": v["LastModified"],
            "size": v["Size"],
            "is_latest": v["IsLatest"],
        }
        for v in response.get("Versions", [])
        if v["Key"] == object_key
    ]


def resolve_restore_dir(owner_id: int, destination: str, job_id: int) -> Path | None:
    """Server-side restore folder: <RESTORE_DIR>/<owner>/<destination>/job-<id>.
    Returns None if `destination` tries to escape (absolute path, '..', drive)."""
    destination = (destination or "").strip().replace("\\", "/")
    if not destination or PurePosixPath(destination).is_absolute() \
            or PureWindowsPath(destination).drive or ".." in PurePosixPath(destination).parts:
        return None
    root = Path(current_app.config["RESTORE_DIR"]).resolve()
    target = (root / str(owner_id) / destination / f"job-{job_id}").resolve()
    if not target.is_relative_to(root / str(owner_id)):
        return None
    return target


def restore_file_to_server(connection, backup: BackupJob, recovery_job) -> bool:
    """Download one backed-up file (exact version) and verify its SHA-256."""
    target_dir = resolve_restore_dir(
        recovery_job.owner_id, recovery_job.destination, recovery_job.id
    )
    if target_dir is None:
        recovery_job.status = "FAILED"
        recovery_job.result_message = "Restore destination is outside the permitted recovery directory."
        recovery_job.completed_at = utcnow()
        db.session.commit()
        return False

    target_dir.mkdir(parents=True, exist_ok=True)
    restored_path = target_dir / Path(backup.object_key).name

    point = recovery_job.recovery_point
    extra_args = {"VersionId": point.version_id} if point and point.version_id else {}

    s3_client = build_session(connection).client("s3")
    s3_client.download_file(
        Bucket=backup.destination_bucket,
        Key=backup.object_key,
        Filename=str(restored_path),
        ExtraArgs=extra_args,
    )

    verified = verify_file_checksum(restored_path, backup.sha256_checksum)
    recovery_job.restored_file = str(restored_path)
    recovery_job.verification_status = "VERIFIED" if verified else "FAILED"
    recovery_job.status = "SUCCEEDED" if verified else "FAILED"
    recovery_job.restored_objects = 1 if verified else 0
    recovery_job.failed_objects = 0 if verified else 1
    recovery_job.bytes_restored = restored_path.stat().st_size if verified else 0
    recovery_job.result_message = (
        "File restored and SHA-256 checksum verified."
        if verified else "Checksum mismatch - the restored file may be corrupt."
    )
    if not verified:
        restored_path.unlink(missing_ok=True)
        recovery_job.restored_file = None
    db.session.commit()
    return verified


# Backwards-compatible name used by older code.
restore_backup = restore_file_to_server


def preserve_file_copy(
    connection, source_path: Path, incident_id: int, display_name: str | None = None
) -> BackupJob:
    if not connection.backup_bucket:
        raise ValueError("No backup bucket is configured for this AWS connection.")

    display_name = safe_filename(display_name or source_path.name)
    backup = BackupJob(
        owner_id=connection.owner_id,
        aws_connection_id=connection.id,
        backup_name=f"Emergency preservation - incident {incident_id}",
        source_type="EMERGENCY_PRESERVATION",
        source_description=f"Preserved copy of {display_name}",
        destination_bucket=connection.backup_bucket,
        status="RUNNING",
        started_at=utcnow(),
    )
    db.session.add(backup)
    db.session.commit()

    try:
        checksum = calculate_sha256(source_path)
        size = source_path.stat().st_size
        object_key = (
            f"emergency-preservation/{connection.owner_id}/"
            f"{incident_id}/{uuid.uuid4().hex}/{display_name}"
        )

        s3_client = build_session(connection).client("s3")
        s3_client.upload_file(
            Filename=str(source_path),
            Bucket=connection.backup_bucket,
            Key=object_key,
            ExtraArgs={"ServerSideEncryption": "AES256", "ChecksumAlgorithm": "SHA256"},
        )
        head = s3_client.head_object(
            Bucket=connection.backup_bucket, Key=object_key, ChecksumMode="ENABLED"
        )
        if head.get("ContentLength") != size:
            raise ValueError("Preserved object size does not match the local file.")

        backup.object_key = object_key
        backup.version_id = head.get("VersionId")
        backup.size_bytes = size
        backup.sha256_checksum = checksum
        backup.s3_checksum = head.get("ChecksumSHA256")
        backup.status = "SUCCEEDED"
        backup.completed_at = utcnow()

        db.session.add(
            RecoveryPoint(
                owner_id=connection.owner_id,
                backup_job_id=backup.id,
                aws_connection_id=connection.id,
                recovery_point_time=backup.completed_at,
                source_resource=display_name,
                backup_type="EMERGENCY_PRESERVATION",
                integrity_status="VERIFIED",
                retention_status="ACTIVE",
                version_id=backup.version_id,
                recovery_eligible=True,
            )
        )
        db.session.commit()
        return backup

    except Exception as error:
        db.session.rollback()
        backup.status = "FAILED"
        backup.error_message = (
            safe_aws_error(error) if isinstance(error, ClientError) else str(error)[:1000]
        )
        backup.completed_at = utcnow()
        db.session.commit()
        raise
