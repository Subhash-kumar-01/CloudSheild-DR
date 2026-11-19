import boto3
from botocore.credentials import RefreshableCredentials
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
from botocore.session import get_session as get_botocore_session
from flask import current_app

from app.extensions import db
from app.models import AwsConnection, SecurityEvent
from app.utils import utcnow

CLIENT_ERROR_MESSAGES = {
    "AccessDenied": "AWS denied access. Check the IAM role trust policy, external ID, and permissions.",
    "InvalidClientTokenId": "AWS rejected CloudShield's own credentials. The administrator must check the server's AWS identity.",
    "ExpiredToken": "The temporary AWS session expired. Try verification again.",
    "NoSuchBucket": "The backup bucket does not exist in this account/region. Deploy the CloudFormation stack (it creates it) or fix the bucket name.",
    "404": "The backup bucket does not exist in this account/region. Deploy the CloudFormation stack (it creates it) or fix the bucket name.",
    "403": "The IAM role cannot access the backup bucket. Check the stack parameters match this connection.",
}


def _assume_role(connection: AwsConnection):
    sts = boto3.client("sts", region_name=connection.region)
    seconds = int(current_app.config.get("AWS_ASSUME_ROLE_SECONDS", 3600))
    return sts.assume_role(
        RoleArn=connection.role_arn,
        RoleSessionName=f"CloudShieldDR-{connection.id}",
        ExternalId=connection.external_id,
        DurationSeconds=seconds,
    )["Credentials"]


def assume_connection_role(connection: AwsConnection):
    return _assume_role(connection)


def build_session(connection: AwsConnection) -> boto3.Session:
    """boto3 session for the customer's account that refreshes its own STS
    credentials, so multi-hour backups/restores don't fail with ExpiredToken."""
    # Copy plain values: this closure may run on worker threads after the
    # SQLAlchemy object (and app context) is gone.
    role_arn, external_id = connection.role_arn, connection.external_id
    region, conn_id = connection.region, connection.id
    seconds = int(current_app.config.get("AWS_ASSUME_ROLE_SECONDS", 3600))

    def refresh():
        creds = boto3.client("sts", region_name=region).assume_role(
            RoleArn=role_arn,
            RoleSessionName=f"CloudShieldDR-{conn_id}",
            ExternalId=external_id,
            DurationSeconds=seconds,
        )["Credentials"]
        return {
            "access_key": creds["AccessKeyId"],
            "secret_key": creds["SecretAccessKey"],
            "token": creds["SessionToken"],
            "expiry_time": creds["Expiration"].isoformat(),
        }

    credentials = RefreshableCredentials.create_from_metadata(
        metadata=refresh(), refresh_using=refresh, method="sts-assume-role"
    )
    botocore_session = get_botocore_session()
    botocore_session._credentials = credentials
    botocore_session.set_config_variable("region", region)
    return boto3.Session(botocore_session=botocore_session)


def check_bucket_access(connection: AwsConnection, bucket: str, session=None) -> tuple[bool, str]:
    """HEAD a bucket through the customer's role."""
    try:
        session = session or build_session(connection)
        session.client("s3").head_bucket(Bucket=bucket)
        return True, "ok"
    except ClientError as error:
        code = str(error.response.get("Error", {}).get("Code", "UnknownError"))
        if code in ("404", "NoSuchBucket"):
            return False, f"Bucket '{bucket}' was not found."
        if code in ("403", "AccessDenied"):
            return False, f"The IAM role cannot access bucket '{bucket}'. Check the SourceBucketScope in your CloudFormation stack."
        return False, f"Could not access bucket '{bucket}' ({code})."
    except Exception as error:  # noqa: BLE001
        current_app.logger.warning("Bucket check failed: %s", type(error).__name__)
        return False, "Could not reach AWS to check the bucket."


def _fail(connection, message, code=None, record_event=False):
    connection.status = "ERROR"
    connection.status_message = message
    db.session.commit()
    if record_event:
        db.session.add(
            SecurityEvent(
                owner_id=connection.owner_id,
                aws_connection_id=connection.id,
                event_category="AWS_CONNECTION_FAILURE",
                severity="HIGH",
                source="APPLICATION",
                affected_resource=connection.role_arn,
                details=f"AWS error code: {code}",
                recommended_response="Review the IAM role trust policy, external ID, and role permissions.",
            )
        )
        db.session.commit()
    return False, message


def verify_connection(connection: AwsConnection) -> tuple[bool, str]:
    try:
        session = build_session(connection)
        identity = session.client("sts").get_caller_identity()

        if identity.get("Account") != connection.aws_account_id:
            return _fail(
                connection,
                "Assumed-role account does not match the configured AWS account ID.",
            )

        warning = ""
        if connection.backup_bucket:
            s3 = session.client("s3")
            s3.head_bucket(Bucket=connection.backup_bucket)
            versioning = s3.get_bucket_versioning(Bucket=connection.backup_bucket)
            if versioning.get("Status") != "Enabled":
                return _fail(
                    connection,
                    "The backup bucket exists but S3 Versioning is not enabled. "
                    "Versioning is required for point-in-time recovery - enable it or "
                    "deploy the CloudFormation stack, which does this for you.",
                )
        else:
            warning = " Set a backup bucket to start protecting data."

        connection.status = "CONNECTED"
        connection.status_message = "AWS connection verified successfully." + warning
        connection.last_verified_at = utcnow()
        db.session.commit()
        return True, connection.status_message

    except ClientError as error:
        code = str(error.response.get("Error", {}).get("Code", "UnknownError"))
        message = CLIENT_ERROR_MESSAGES.get(
            code,
            "AWS connection verification failed. Check the role ARN, external ID, region, and IAM permissions.",
        )
        return _fail(connection, message, code, record_event=True)

    except NoCredentialsError:
        return _fail(
            connection,
            "CloudShield's own AWS credentials are not configured on the server. "
            "The administrator must give the app an AWS identity allowed to call sts:AssumeRole.",
        )

    except BotoCoreError as error:
        current_app.logger.warning("AWS verification error: %s", type(error).__name__)
        return _fail(
            connection,
            "Could not reach AWS. Check the server's network access and the selected region.",
        )
