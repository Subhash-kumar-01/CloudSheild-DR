"""Minimal in-memory S3 that mimics the behaviours the sync engine relies on:
sorted listings, per-key version history (newest first), and server-side copy
creating a new version stamped with the copy time."""
from datetime import datetime, timedelta, timezone
import itertools


class FakeClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    def __init__(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.buckets = {}  # bucket -> key -> [version dicts, newest first]
        self.versioned = set()
        self._ids = itertools.count(1)
        self.fail_keys = {}  # key -> error code
        self.copy_calls = []

    def tick(self, **delta):
        self.now += timedelta(**(delta or {"minutes": 1}))
        return self.now

    def create_bucket(self, name, versioned=False):
        self.buckets[name] = {}
        if versioned:
            self.versioned.add(name)

    def put(self, bucket, key, data=b"x", storage_class="STANDARD"):
        self._store(bucket, key, data, storage_class)

    def _store(self, bucket, key, data, storage_class):
        versions = self.buckets[bucket].setdefault(key, [])
        vid = f"v{next(self._ids)}" if bucket in self.versioned else "null"
        entry = {"Key": key, "VersionId": vid, "LastModified": self.now,
                 "Size": len(data), "Data": data, "StorageClass": storage_class}
        if bucket in self.versioned:
            versions.insert(0, entry)
        else:
            versions[:] = [entry]

    def delete(self, bucket, key):
        del self.buckets[bucket][key]

    def keys(self, bucket):
        return sorted(self.buckets[bucket])

    def latest(self, bucket, key):
        return self.buckets[bucket][key][0]

    # ---- boto3-style API used by the engine ----
    def get_paginator(self, op):
        fake = self

        class _P:
            def paginate(self, Bucket, Prefix=""):
                if op == "list_objects_v2":
                    items = [dict(v[0]) for k, v in sorted(fake.buckets[Bucket].items())
                             if k.startswith(Prefix)]
                    for i in range(0, max(len(items), 1), 3):  # small pages on purpose
                        yield {"Contents": items[i:i + 3]}
                else:
                    items = []
                    for k, vs in sorted(fake.buckets[Bucket].items()):
                        if k.startswith(Prefix):
                            items.extend(dict(v) for v in vs)
                    for i in range(0, max(len(items), 1), 3):
                        yield {"Versions": items[i:i + 3]}
        return _P()

    def copy(self, CopySource, Bucket, Key, ExtraArgs=None, Config=None):
        self.copy_calls.append((CopySource, Bucket, Key, ExtraArgs))
        if CopySource["Key"] in self.fail_keys:
            raise FakeClientError(self.fail_keys[CopySource["Key"]])
        versions = self.buckets[CopySource["Bucket"]][CopySource["Key"]]
        if "VersionId" in CopySource:
            src = next(v for v in versions if v["VersionId"] == CopySource["VersionId"])
        else:
            src = versions[0]
        storage = (ExtraArgs or {}).get("StorageClass", "STANDARD")
        self._store(Bucket, Key, src["Data"], storage)
