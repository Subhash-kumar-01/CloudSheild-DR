"""Bucket-to-bucket backup and point-in-time restore engine.

Design goals (in priority order):
  1. Never delete or overwrite anything in the customer's account.
  2. Be cheap: all data moves with server-side S3 copies (no bytes pass through
     CloudShield, no data-transfer fees inside a region) and changes are
     detected from LIST responses only (no per-object HEAD/GET requests).
  3. Constant memory: source and backup listings are both sorted, so they are
     merge-joined as streams instead of being loaded into dicts.

This module deliberately has NO third-party imports so it can be unit-tested
with a tiny fake S3 client. It only needs a boto3-style client object.
"""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable, Iterator, Optional

MIRROR_ROOT = "mirror"
ARCHIVED_CLASSES = {"GLACIER", "DEEP_ARCHIVE"}
MAX_RECORDED_ERRORS = 15


# --------------------------------------------------------------------------
# Key layout helpers
# --------------------------------------------------------------------------
def normalize_prefix(prefix: Optional[str]) -> str:
    """'' for whole bucket, otherwise 'a/b/' (no leading slash, trailing slash)."""
    prefix = (prefix or "").strip().lstrip("/")
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return prefix


def mirror_prefix(source_bucket: str) -> str:
    return f"{MIRROR_ROOT}/{source_bucket}/"


def mirror_key(source_bucket: str, key: str) -> str:
    return mirror_prefix(source_bucket) + key


def original_key(source_bucket: str, backup_key: str) -> str:
    return backup_key[len(mirror_prefix(source_bucket)):]


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Listing helpers (work with any boto3-style client)
# --------------------------------------------------------------------------
def iter_objects(s3, bucket: str, prefix: str = "") -> Iterator[dict]:
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        yield from page.get("Contents", [])


def iter_versions(s3, bucket: str, prefix: str = "") -> Iterator[dict]:
    paginator = s3.get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        yield from page.get("Versions", [])


# --------------------------------------------------------------------------
# Pure planning logic
# --------------------------------------------------------------------------
def needs_copy(source_obj: dict, backup_obj: dict) -> bool:
    """True when the backup copy is stale.

    A backup copy's LastModified is the time *we* copied it. If the source was
    modified after that, or the size differs, the backup is out of date.
    """
    if source_obj["Size"] != backup_obj["Size"]:
        return True
    return _aware(backup_obj["LastModified"]) < _aware(source_obj["LastModified"])


def diff_listings(
    source_iter: Iterable[dict],
    backup_iter: Iterable[dict],
    source_bucket: str,
) -> Iterator[tuple[str, dict]]:
    """Merge-join two lexicographically sorted listings.

    Yields (action, obj) where action is one of:
      'new'        source object not yet in backup          (obj = source obj)
      'changed'    source object newer than backup copy     (obj = source obj)
      'unchanged'  backup is current                        (obj = source obj)
      'missing'    backup object whose source is gone       (obj = backup obj)
    """
    prefix_len = len(mirror_prefix(source_bucket))
    src, bak = iter(source_iter), iter(backup_iter)
    s, b = next(src, None), next(bak, None)

    while s is not None or b is not None:
        if b is None:
            yield "new", s
            s = next(src, None)
            continue
        if s is None:
            yield "missing", b
            b = next(bak, None)
            continue

        b_orig = b["Key"][prefix_len:]
        if s["Key"] < b_orig:
            yield "new", s
            s = next(src, None)
        elif s["Key"] > b_orig:
            yield "missing", b
            b = next(bak, None)
        else:
            yield ("changed" if needs_copy(s, b) else "unchanged"), s
            s, b = next(src, None), next(bak, None)


def pick_versions_as_of(version_iter: Iterable[dict], as_of: datetime) -> Iterator[dict]:
    """For each key choose the newest version with LastModified <= as_of.

    S3 lists versions sorted by key, newest first within a key, so the first
    qualifying version per key wins.
    """
    cutoff = _aware(as_of)
    current_key = None
    chosen = False
    for version in version_iter:
        if version["Key"] != current_key:
            current_key, chosen = version["Key"], False
        if chosen:
            continue
        if _aware(version["LastModified"]) <= cutoff:
            chosen = True
            yield version


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------
@dataclass
class SyncResult:
    scanned: int = 0
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    archived_skipped: int = 0
    missing_at_source: int = 0
    backup_objects: int = 0
    copied: int = 0
    failed: int = 0
    bytes_copied: int = 0
    errors: list = field(default_factory=list)

    def record_error(self, key: str, exc: Exception):
        self.failed += 1
        if len(self.errors) < MAX_RECORDED_ERRORS:
            self.errors.append(f"{key}: {describe_error(exc)}")


@dataclass
class RestoreResult:
    selected: int = 0
    restored: int = 0
    failed: int = 0
    bytes_restored: int = 0
    errors: list = field(default_factory=list)

    def record_error(self, key: str, exc: Exception):
        self.failed += 1
        if len(self.errors) < MAX_RECORDED_ERRORS:
            self.errors.append(f"{key}: {describe_error(exc)}")


def describe_error(exc: Exception) -> str:
    """Short, safe error text (no credentials, no stack traces)."""
    code = getattr(exc, "response", {}).get("Error", {}).get("Code") if hasattr(exc, "response") else None
    hints = {
        "AccessDenied": "access denied (check IAM role permissions; SSE-KMS buckets need kms:Decrypt)",
        "InvalidObjectState": "object is archived (Glacier/Deep Archive) and must be restored first",
        "NoSuchKey": "object disappeared during the run",
        "NoSuchBucket": "bucket does not exist",
        "ExpiredToken": "temporary AWS session expired",
    }
    if code:
        return hints.get(code, f"AWS error {code}")
    return type(exc).__name__


# --------------------------------------------------------------------------
# Engines
# --------------------------------------------------------------------------
def _run_bounded(tasks: Iterable[Callable[[], object]], workers: int, on_done: Callable):
    """Run callables on a thread pool with a bounded in-flight window."""
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending: dict = {}
        for label, size, fn in tasks:
            pending[pool.submit(fn)] = (label, size)
            if len(pending) >= workers * 4:
                done, _ = wait(list(pending), return_when=FIRST_COMPLETED)
                for fut in done:
                    on_done(fut, *pending.pop(fut))
        for fut in list(pending):
            on_done(fut, *pending.pop(fut))


def run_sync(
    s3,
    source_bucket: str,
    source_prefix: str,
    backup_bucket: str,
    storage_class: str = "INTELLIGENT_TIERING",
    workers: int = 8,
    transfer_config=None,
) -> SyncResult:
    """Copy new/changed objects from source_bucket into backup_bucket.

    Nothing is ever deleted. The backup bucket must have versioning enabled so
    every overwritten version stays recoverable.
    """
    result = SyncResult()
    source_prefix = normalize_prefix(source_prefix)

    def classified() -> Iterator[tuple[str, dict]]:
        source_iter = iter_objects(s3, source_bucket, source_prefix)
        backup_iter = iter_objects(
            s3, backup_bucket, mirror_prefix(source_bucket) + source_prefix
        )
        for action, obj in diff_listings(source_iter, backup_iter, source_bucket):
            if action == "missing":
                result.backup_objects += 1
                result.missing_at_source += 1
            else:
                result.scanned += 1
                if action != "new":
                    result.backup_objects += 1
                setattr(result, action, getattr(result, action) + 1)
            yield action, obj

    def tasks():
        for action, obj in classified():
            if action not in ("new", "changed"):
                continue
            if obj.get("StorageClass") in ARCHIVED_CLASSES:
                result.archived_skipped += 1
                continue
            key = obj["Key"]
            extra = {"ServerSideEncryption": "AES256", "StorageClass": storage_class}
            kwargs = {"ExtraArgs": extra}
            if transfer_config is not None:
                kwargs["Config"] = transfer_config

            def do_copy(key=key, kwargs=kwargs):
                s3.copy(
                    {"Bucket": source_bucket, "Key": key},
                    backup_bucket,
                    mirror_key(source_bucket, key),
                    **kwargs,
                )

            yield key, obj["Size"], do_copy

    def on_done(future, key, size):
        try:
            future.result()
            result.copied += 1
            result.bytes_copied += size
        except Exception as exc:  # one bad object must not abort the whole run
            result.record_error(key, exc)

    _run_bounded(tasks(), workers, on_done)
    return result


def run_restore_as_of(
    s3,
    backup_bucket: str,
    source_bucket: str,
    source_prefix: str,
    as_of: datetime,
    dest_bucket: str,
    dest_prefix: str,
    workers: int = 8,
    transfer_config=None,
) -> RestoreResult:
    """Restore the state of source_bucket/source_prefix as of `as_of`.

    Objects are copied (server-side) from the versioned backup bucket into
    dest_bucket under dest_prefix. Originals are never touched.
    """
    result = RestoreResult()
    source_prefix = normalize_prefix(source_prefix)
    dest_prefix = normalize_prefix(dest_prefix)

    versions = iter_versions(s3, backup_bucket, mirror_prefix(source_bucket) + source_prefix)

    def tasks():
        for version in pick_versions_as_of(versions, as_of):
            result.selected += 1
            backup_key = version["Key"]
            dest_key = dest_prefix + original_key(source_bucket, backup_key)
            kwargs = {"ExtraArgs": {"ServerSideEncryption": "AES256"}}
            if transfer_config is not None:
                kwargs["Config"] = transfer_config

            def do_copy(backup_key=backup_key, version_id=version["VersionId"],
                        dest_key=dest_key, kwargs=kwargs):
                s3.copy(
                    {"Bucket": backup_bucket, "Key": backup_key, "VersionId": version_id},
                    dest_bucket,
                    dest_key,
                    **kwargs,
                )

            yield backup_key, version["Size"], do_copy

    def on_done(future, key, size):
        try:
            future.result()
            result.restored += 1
            result.bytes_restored += size
        except Exception as exc:
            result.record_error(key, exc)

    _run_bounded(tasks(), workers, on_done)
    return result


# --------------------------------------------------------------------------
# Anomaly heuristics (ransomware / mass-deletion early warning)
# --------------------------------------------------------------------------
def detect_anomalies(result: SyncResult) -> list[dict]:
    """Return a list of {category, severity, details, response} dicts."""
    findings = []
    if result.backup_objects:
        deleted_ratio = result.missing_at_source / result.backup_objects
        if result.missing_at_source >= 10 and deleted_ratio >= 0.2:
            findings.append({
                "category": "MASS_DELETION_SUSPECTED",
                "severity": "HIGH",
                "details": (
                    f"{result.missing_at_source} of {result.backup_objects} previously "
                    f"backed-up objects ({deleted_ratio:.0%}) no longer exist in the source bucket."
                ),
                "response": (
                    "Check CloudTrail for the deleting principal and rotate its credentials. "
                    "Your data is still in the backup bucket - open an Emergency incident to "
                    "restore from a point before the deletion."
                ),
            })
    existing = result.changed + result.unchanged
    if existing:
        changed_ratio = result.changed / existing
        if result.changed >= 20 and changed_ratio >= 0.5:
            findings.append({
                "category": "MASS_MODIFICATION_SUSPECTED",
                "severity": "HIGH",
                "details": (
                    f"{result.changed} of {existing} existing objects ({changed_ratio:.0%}) "
                    "were modified since the last backup, which can indicate ransomware "
                    "encryption or a bad deployment."
                ),
                "response": (
                    "Investigate before the next retention cycle expires older versions. "
                    "Earlier versions are preserved - open an Emergency incident and restore "
                    "from a recovery point before the change."
                ),
            })
    return findings
