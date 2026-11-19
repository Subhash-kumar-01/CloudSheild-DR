import importlib.util
import os
import sys
import unittest
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from fake_s3 import FakeS3  # noqa: E402

# The engine has no third-party imports, so load it straight from its file.
# This keeps these tests runnable without the Flask/SQLAlchemy/boto3 stack.
_spec = importlib.util.spec_from_file_location(
    "s3_sync", os.path.join(HERE, "..", "app", "services", "s3_sync.py"))
eng = importlib.util.module_from_spec(_spec)
sys.modules["s3_sync"] = eng
_spec.loader.exec_module(eng)

UTC = timezone.utc


def new_env():
    s3 = FakeS3()
    s3.create_bucket("src")
    s3.create_bucket("bak", versioned=True)
    return s3


class PrefixTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(eng.normalize_prefix(None), "")
        self.assertEqual(eng.normalize_prefix("/a/b"), "a/b/")
        self.assertEqual(eng.normalize_prefix("a/"), "a/")

    def test_mirror_roundtrip(self):
        self.assertEqual(eng.mirror_key("src", "a/b.txt"), "mirror/src/a/b.txt")
        self.assertEqual(eng.original_key("src", "mirror/src/a/b.txt"), "a/b.txt")


class DiffTests(unittest.TestCase):
    def actions(self, src, bak):
        return [(a, o["Key"]) for a, o in eng.diff_listings(src, bak, "src")]

    def obj(self, key, size=1, minute=0):
        return {"Key": key, "Size": size,
                "LastModified": datetime(2026, 1, 1, 0, minute, tzinfo=UTC)}

    def test_all_cases_in_one_merge(self):
        src = [self.obj("a", minute=5), self.obj("b"), self.obj("c", size=9), self.obj("e")]
        bak = [self.obj("mirror/src/a", minute=1), self.obj("mirror/src/b", minute=9),
               self.obj("mirror/src/c", size=1, minute=9), self.obj("mirror/src/d", minute=9)]
        self.assertEqual(
            self.actions(src, bak),
            [("changed", "a"), ("unchanged", "b"), ("changed", "c"),
             ("missing", "mirror/src/d"), ("new", "e")],
        )

    def test_empty_sides(self):
        self.assertEqual(self.actions([], []), [])
        self.assertEqual(self.actions([self.obj("a")], []), [("new", "a")])
        self.assertEqual(self.actions([], [self.obj("mirror/src/a")]),
                         [("missing", "mirror/src/a")])

    def test_unicode_and_prefix_ordering(self):
        keys = sorted(["a", "a b", "a/b", "a-b", "é", "z"])
        src = [self.obj(k) for k in keys]
        bak = [self.obj("mirror/src/" + k, minute=9) for k in keys]
        self.assertTrue(all(a == "unchanged" for a, _ in eng.diff_listings(src, bak, "src")))


class PickVersionsTests(unittest.TestCase):
    def v(self, key, vid, minute):
        return {"Key": key, "VersionId": vid, "Size": 1,
                "LastModified": datetime(2026, 1, 1, 0, minute, tzinfo=UTC)}

    def test_newest_not_after_cutoff(self):
        versions = [self.v("a", "a3", 30), self.v("a", "a2", 20), self.v("a", "a1", 10),
                    self.v("b", "b2", 40), self.v("c", "c1", 5)]
        cutoff = datetime(2026, 1, 1, 0, 25)  # naive treated as UTC
        picked = {v["Key"]: v["VersionId"] for v in eng.pick_versions_as_of(versions, cutoff)}
        self.assertEqual(picked, {"a": "a2", "c": "c1"})  # b did not exist yet


class SyncTests(unittest.TestCase):
    def sync(self, s3, prefix=""):
        return eng.run_sync(s3, "src", prefix, "bak", workers=3)

    def test_first_sync_copies_everything_with_cost_settings(self):
        s3 = new_env()
        for i in range(7):
            s3.put("src", f"dir/f{i}.txt", b"data" * i)
        s3.tick()
        r = self.sync(s3)
        self.assertEqual((r.new, r.copied, r.failed), (7, 7, 0))
        self.assertEqual(s3.keys("bak"), [f"mirror/src/dir/f{i}.txt" for i in range(7)])
        extra = s3.copy_calls[0][3]
        self.assertEqual(extra["StorageClass"], "INTELLIGENT_TIERING")
        self.assertEqual(extra["ServerSideEncryption"], "AES256")

    def test_second_sync_is_a_noop_and_never_deletes(self):
        s3 = new_env()
        s3.put("src", "a", b"1")
        s3.put("src", "b", b"2")
        s3.tick()
        self.sync(s3)
        s3.tick()
        n = len(s3.copy_calls)
        r = self.sync(s3)
        self.assertEqual((r.unchanged, r.copied), (2, 0))
        self.assertEqual(len(s3.copy_calls), n)

    def test_change_creates_new_version_old_kept(self):
        s3 = new_env()
        s3.put("src", "a", b"one")
        s3.tick()
        self.sync(s3)
        s3.tick()
        s3.put("src", "a", b"two!")
        s3.tick()
        r = self.sync(s3)
        self.assertEqual((r.changed, r.copied), (1, 1))
        self.assertEqual(len(s3.buckets["bak"]["mirror/src/a"]), 2)

    def test_deleted_at_source_is_kept_and_counted(self):
        s3 = new_env()
        for k in "abc":
            s3.put("src", k)
        s3.tick()
        self.sync(s3)
        s3.delete("src", "b")
        s3.tick()
        r = self.sync(s3)
        self.assertEqual(r.missing_at_source, 1)
        self.assertIn("mirror/src/b", s3.keys("bak"))

    def test_prefix_scoping(self):
        s3 = new_env()
        s3.put("src", "keep/a")
        s3.put("src", "other/b")
        s3.tick()
        r = self.sync(s3, prefix="keep")
        self.assertEqual(r.copied, 1)
        self.assertEqual(s3.keys("bak"), ["mirror/src/keep/a"])

    def test_failures_are_recorded_but_do_not_abort(self):
        s3 = new_env()
        for k in "abcd":
            s3.put("src", k)
        s3.fail_keys["b"] = "AccessDenied"
        s3.tick()
        r = self.sync(s3)
        self.assertEqual((r.copied, r.failed), (3, 1))
        self.assertIn("access denied", r.errors[0])
        # next run retries only the failed object
        s3.fail_keys.clear()
        s3.tick()
        r2 = self.sync(s3)
        self.assertEqual((r2.copied, r2.unchanged), (1, 3))

    def test_archived_objects_are_skipped_not_failed(self):
        s3 = new_env()
        s3.put("src", "cold", storage_class="DEEP_ARCHIVE")
        s3.put("src", "hot")
        s3.tick()
        r = self.sync(s3)
        self.assertEqual((r.copied, r.archived_skipped, r.failed), (1, 1, 0))


class RestoreTests(unittest.TestCase):
    def test_point_in_time_restore_recovers_pre_incident_state(self):
        s3 = new_env()
        s3.put("src", "report.docx", b"GOOD")
        s3.put("src", "notes.txt", b"notes")
        s3.tick()
        eng.run_sync(s3, "src", "", "bak")
        good_time = s3.tick()                      # recovery point
        s3.tick()
        s3.put("src", "report.docx", b"ENCRYPTED-BY-RANSOMWARE")  # attack
        s3.delete("src", "notes.txt")
        s3.put("src", "ransom.txt", b"pay up")
        s3.tick()
        eng.run_sync(s3, "src", "", "bak")         # sync after the attack

        r = eng.run_restore_as_of(s3, "bak", "src", "", good_time,
                                  "src", "cloudshield-restore/job-1/")
        self.assertEqual((r.selected, r.restored, r.failed), (2, 2, 0))
        self.assertEqual(s3.latest("src", "cloudshield-restore/job-1/report.docx")["Data"], b"GOOD")
        self.assertEqual(s3.latest("src", "cloudshield-restore/job-1/notes.txt")["Data"], b"notes")
        self.assertNotIn("cloudshield-restore/job-1/ransom.txt", s3.keys("src"))
        # the live (attacked) object was never overwritten
        self.assertEqual(s3.latest("src", "report.docx")["Data"], b"ENCRYPTED-BY-RANSOMWARE")

    def test_restore_pins_exact_version(self):
        s3 = new_env()
        s3.put("src", "a", b"v1")
        s3.tick()
        eng.run_sync(s3, "src", "", "bak")
        t = s3.tick()
        s3.tick()
        s3.put("src", "a", b"v2")
        s3.tick()
        eng.run_sync(s3, "src", "", "bak")
        eng.run_restore_as_of(s3, "bak", "src", "", t, "src", "r/")
        self.assertIn("VersionId", s3.copy_calls[-1][0])


class AnomalyTests(unittest.TestCase):
    def test_quiet_run_has_no_findings(self):
        self.assertEqual(eng.detect_anomalies(eng.SyncResult(unchanged=100, backup_objects=100)), [])

    def test_mass_deletion(self):
        r = eng.SyncResult(missing_at_source=30, backup_objects=100, unchanged=70)
        self.assertEqual(eng.detect_anomalies(r)[0]["category"], "MASS_DELETION_SUSPECTED")

    def test_mass_modification(self):
        r = eng.SyncResult(changed=60, unchanged=40, backup_objects=100)
        self.assertEqual(eng.detect_anomalies(r)[0]["category"], "MASS_MODIFICATION_SUSPECTED")

    def test_small_buckets_do_not_alarm(self):
        r = eng.SyncResult(missing_at_source=3, backup_objects=4, changed=3, unchanged=1)
        self.assertEqual(eng.detect_anomalies(r), [])


if __name__ == "__main__":
    unittest.main()
