<<<<<<< HEAD
# CloudSheild-DR
=======
<<<<<<< HEAD
# RecoverXcloud
A Cloud-Based Disaster Recovery System built using AWS, Python, Flask, MySQL, and Docker. Provides secure cloud backups and file restoration to help prevent data loss, reduce downtime, and improve data availability.
=======
# CloudShield DR (RecoverXcloud)

A low-cost disaster-recovery platform for AWS. A customer connects their AWS account, CloudShield
continuously copies their S3 data into a **versioned, encrypted backup bucket inside the customer's own
account**, and if something goes wrong (accidental deletion, overwrite, ransomware) the customer can
restore their data **as it was at any earlier point in time**.

## What it does

| Feature | How |
|---|---|
| Connect an AWS account | Customer deploys one CloudFormation stack (creates backup bucket + least-privilege IAM role). CloudShield assumes the role with a per-connection **External ID**. No AWS keys are ever stored. |
| Scheduled bucket backups | Worker copies new/changed objects `source bucket -> backup bucket` using server-side S3 copies. Hourly / daily / weekly. Nothing is ever deleted. |
| Point-in-time recovery | Backup bucket is versioned; a restore rebuilds the bucket "as of" a recovery point into `s3://<source>/cloudshield-restore/job-N/` - live data is never overwritten. |
| File backups | Upload a file; stored with SHA-256 + S3 checksum; restore = verified download. |
| Emergency recovery | Report an incident, preserve remaining data, pick a recommended pre-incident recovery point, recover. Tracks measured recovery time and data-loss window. |
| Ransomware early warning | Each sync flags mass deletions / mass modifications as security events. |
| Audit + security events | Every sensitive action is logged; login throttling; CSRF; strict CSP. |

## Why it is cheap

* **No data flows through the app.** Backups and restores are S3-to-S3 server-side copies (no transfer fees inside a region, no big server needed).
* **Change detection uses LIST calls only** (no per-object GET/HEAD) and streams both listings, so memory use is constant even for millions of objects.
* **Intelligent-Tiering** backup storage by default (no retrieval fee, no minimum duration); selectable per schedule: Glacier Instant Retrieval, Standard-IA, Standard.
* **Lifecycle rule** expires old versions after your retention period (default 30 days) - you pay for history only as far back as you choose. Incomplete multipart uploads are cleaned after 7 days.
* **SSE-S3 encryption** (free; no KMS request charges).
* **SQLite by default** - no database server. The whole platform runs on one small instance (2 containers: web + worker). MySQL is optional.
* Hourly is the minimum schedule frequency, to bound LIST-request costs.

Your AWS bill is dominated by storage of the backup bucket: roughly (size of your data + size of changes kept within the retention window) x the storage-class price. Use the AWS Pricing Calculator with your region's prices; see "Tuning cost" below.

## Quick start (single small server)

```bash
cp .env.example .env
# edit .env: SECRET_KEY (python -c "import secrets;print(secrets.token_hex(32))"), CLOUDSHIELD_AWS_ACCOUNT_ID
docker compose up -d --build
# open http://<server>:8000  (put HTTPS in front for real use, then set SESSION_COOKIE_SECURE=true)
```

Local development without Docker:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env     # set FLASK_ENV=development
flask --app wsgi run     # terminal 1 (creates the SQLite DB automatically)
python worker.py         # terminal 2 (scheduled backups; optional - "Run now" and restores also work without it)
```

### Give CloudShield an AWS identity (one-time, platform operator)

CloudShield needs permission to *assume the customers' roles* and nothing else
(`infrastructure/iam/cloudshield-platform-policy.json`).

* **EC2**: attach an instance profile with that policy; leave `AWS_ACCESS_KEY_ID` empty.
* **Other hosts (Lightsail, VPS...)**: create an IAM user with only that policy and put its keys in `.env`.

`CLOUDSHIELD_AWS_ACCOUNT_ID` must be the account that identity lives in.

## Customer flow

1. Register -> **AWS Accounts -> Connect**. Enter account ID, region, and (optionally) which buckets CloudShield may read.
2. The connection page shows a ready-to-run `aws cloudformation deploy ...` command with the External ID filled in. Run it in CloudShell.
3. Click **Verify connection** (checks the role, the account, and that the backup bucket exists and has versioning on).
4. **Schedules -> New schedule**: pick a bucket, folder and frequency. The first backup starts immediately.
5. After an incident: **Emergency -> Report incident**. Choose the recommended recovery point and click **Recover**. Review the files in `cloudshield-restore/job-N/` and copy back what you need.

## Security model

* Customer role trust policy requires the per-connection External ID (confused-deputy protection); external IDs are random and unique.
* Role permissions: read source buckets, read/write the backup bucket, write **only** `cloudshield-restore/*` in source buckets. `s3:DeleteObject*`, versioning/lifecycle/policy changes and Object-Lock bypass are **explicitly denied** - a compromised CloudShield cannot destroy backups.
* Optional `ObjectLockDays` parameter locks backup versions (Governance mode) against deletion by anyone with stolen credentials.
* Backup bucket: private, versioned, TLS-only, bucket-owner-enforced, `DeletionPolicy: Retain` (deleting the stack never deletes your backups).
* App: CSRF on all forms, per-user data isolation on every query, login throttling, safe redirects, restore paths confined per user, strict CSP, security headers, production refuses to start with a default `SECRET_KEY`.

## Limitations (please read)

* **Protects Amazon S3 data.** RDS, EBS, DynamoDB etc. are not backed up by this version. (Enable RDS automated backups / DynamoDB PITR / AWS Backup for those; adding them is a natural next step.)
* Restores are written to `cloudshield-restore/` (by design, so live data can never be overwritten); the customer copies data back.
* The backup bucket lives in the same account/region as the source. For protection against account compromise or regional outage, enable `ObjectLockDays` and/or replicate the backup bucket yourself.
* Objects in S3 Glacier/Deep Archive storage classes are skipped (and counted) until restored by the owner.
* A recovery point is valid only within the retention window (older ones are marked expired automatically).
* Source buckets encrypted with SSE-KMS need the `SourceKmsKeyArn` stack parameter.
* File uploads through the web form are capped by `MAX_BACKUP_SIZE_MB` (default 512). Use bucket schedules for large data.

## Tuning cost

* Many tiny files (< 128 KB): choose **Standard** for that schedule (Intelligent-Tiering/IA bill a 128 KB minimum per object).
* Large, rarely-changing data: **Glacier Instant Retrieval** is cheapest per GB (90-day minimum charge applies to deleted/expired versions).
* Lower `RetentionDays`, or narrow `SourceBucketScope` / use prefixes to back up only what matters.
* Use `DAILY`/`WEEKLY` unless your RPO needs hourly.

## Tests

```bash
python -m unittest tests.test_s3_sync -v   # engine tests, no extra dependencies
pytest                                      # full suite (needs requirements-dev.txt)
```

`tests/test_s3_sync.py` exercises the backup/restore engine against an in-memory S3 fake, including a ransomware scenario
(attack -> sync -> point-in-time restore to the pre-attack state).

## Project layout

```
app/routes/          web pages (auth, dashboard, aws_connections, schedules, backups, incidents)
app/services/        s3_sync (pure engine), schedule_service, emergency_recovery_service, job_runner, ...
app/models/          SQLAlchemy models
infrastructure/      CloudFormation template for the customer account + IAM policy for the platform
worker.py            background worker (scheduled backups, pending recoveries, retention, stale-job cleanup)

>>>>>>> f807f5d (project commit)
