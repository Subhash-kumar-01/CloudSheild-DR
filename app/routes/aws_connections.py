import re
import shlex
from pathlib import Path

from flask import (
    Blueprint, current_app, flash, redirect, render_template, send_file, url_for
)
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from wtforms import IntegerField, SelectField, StringField
from wtforms.validators import DataRequired, Length, NumberRange, Optional, Regexp

from app.extensions import db
from app.models import AwsConnection
from app.services.aws_connection_service import verify_connection
from app.utils import audit, is_valid_bucket_name

aws_connections_bp = Blueprint("aws_connections", __name__, url_prefix="/aws")

AWS_REGIONS = [
    ("ap-south-1", "Asia Pacific (Mumbai)"),
    ("ap-southeast-1", "Asia Pacific (Singapore)"),
    ("us-east-1", "US East (N. Virginia)"),
    ("us-east-2", "US East (Ohio)"),
    ("us-west-2", "US West (Oregon)"),
    ("eu-west-1", "Europe (Ireland)"),
    ("eu-central-1", "Europe (Frankfurt)"),
]

SCOPE_RE = re.compile(r"^[a-z0-9.*?-]{1,255}$")
TEMPLATE_NAME = "cloudshield-backup-role.yaml"


class ConnectAwsForm(FlaskForm):
    display_name = StringField(
        "Connection name", validators=[DataRequired(), Length(max=120)]
    )
    aws_account_id = StringField(
        "AWS account ID",
        validators=[
            DataRequired(),
            Regexp(r"^\d{12}$", message="AWS account ID must contain exactly 12 digits."),
        ],
    )
    region = SelectField("AWS region", choices=AWS_REGIONS, validators=[DataRequired()])
    role_arn = StringField(
        "IAM role ARN (optional)", validators=[Optional(), Length(max=600)]
    )
    backup_bucket = StringField(
        "Backup bucket name (optional)", validators=[Optional(), Length(max=255)]
    )
    source_scope = StringField(
        "Buckets CloudShield may read",
        default="*",
        validators=[Optional(), Length(max=255)],
    )
    retention_days = IntegerField(
        "Keep old versions for (days)",
        default=30,
        validators=[DataRequired(), NumberRange(min=1, max=365)],
    )


class BucketForm(FlaskForm):
    backup_bucket = StringField(
        "Private S3 backup bucket name",
        validators=[DataRequired(), Length(max=255)],
    )


def _own_connection(connection_id):
    return AwsConnection.query.filter_by(
        id=connection_id, owner_id=current_user.id
    ).first_or_404()


def _cloudformation_command(connection: AwsConnection) -> str:
    role_name = connection.role_arn.rsplit("/", 1)[-1]
    platform_account = current_app.config["CLOUDSHIELD_AWS_ACCOUNT_ID"] or "<CLOUDSHIELD_AWS_ACCOUNT_ID>"
    params = [
        f"CloudShieldAccountId={platform_account}",
        f"ExternalId={connection.external_id}",
        f"BackupBucketName={connection.backup_bucket or '<choose-a-unique-bucket-name>'}",
        f"SourceBucketScope={connection.source_scope}",
        f"RetentionDays={connection.retention_days}",
        f"RoleName={role_name}",
    ]
    lines = [
        "aws cloudformation deploy",
        f"  --region {connection.region}",
        "  --stack-name cloudshield-dr",
        f"  --template-file {TEMPLATE_NAME}",
        "  --capabilities CAPABILITY_NAMED_IAM",
        "  --parameter-overrides",
    ] + [f"    {shlex.quote(p)}" for p in params]
    return " \\\n".join(lines)


@aws_connections_bp.route("/accounts")
@login_required
def list_accounts():
    connections = (
        AwsConnection.query.filter_by(owner_id=current_user.id)
        .order_by(AwsConnection.created_at.desc())
        .all()
    )
    return render_template(
        "aws/accounts.html", connections=connections, form=ConnectAwsForm()
    )


@aws_connections_bp.route("/accounts/connect", methods=["POST"])
@login_required
def connect_account():
    form = ConnectAwsForm()
    if not form.validate_on_submit():
        for field, errors in form.errors.items():
            for error in errors:
                flash(f"{getattr(form, field).label.text}: {error}", "danger")
        return redirect(url_for("aws_connections.list_accounts"))

    account_id = form.aws_account_id.data.strip()
    role_arn = (form.role_arn.data or "").strip() or AwsConnection.default_role_arn(
        account_id, current_app.config["CLOUDSHIELD_BACKUP_ROLE_NAME"]
    )
    bucket = (form.backup_bucket.data or "").strip().lower() or AwsConnection.suggest_bucket_name(account_id)
    scope = (form.source_scope.data or "*").strip().lower() or "*"

    if not AwsConnection.is_valid_account_id(account_id):
        flash("Invalid AWS account ID.", "danger")
    elif not AwsConnection.is_valid_role_arn(role_arn):
        flash("Invalid IAM role ARN format.", "danger")
    elif AwsConnection.account_from_role_arn(role_arn) != account_id:
        flash("The IAM role ARN account does not match the entered AWS account ID.", "danger")
    elif not is_valid_bucket_name(bucket):
        flash("Invalid bucket name. Use 3-63 lowercase letters, digits, dots or hyphens.", "danger")
    elif not SCOPE_RE.fullmatch(scope):
        flash("Source scope may only contain lowercase letters, digits, '.', '-', '*' and '?'.", "danger")
    else:
        existing = AwsConnection.query.filter_by(
            owner_id=current_user.id, aws_account_id=account_id
        ).first()
        if existing:
            flash("This AWS account is already connected.", "warning")
            return redirect(
                url_for("aws_connections.connection_detail", connection_id=existing.id)
            )

        connection = AwsConnection(
            owner_id=current_user.id,
            display_name=form.display_name.data.strip(),
            aws_account_id=account_id,
            region=form.region.data,
            role_arn=role_arn,
            external_id=AwsConnection.generate_external_id(),
            status="PENDING",
            status_message="Deploy the CloudFormation stack, then verify the connection.",
            backup_bucket=bucket,
            source_scope=scope,
            retention_days=form.retention_days.data,
        )
        db.session.add(connection)
        db.session.flush()
        audit("AWS_CONNECTION_CREATED", "aws_connection", connection.id)
        db.session.commit()
        return redirect(
            url_for("aws_connections.connection_detail", connection_id=connection.id)
        )

    return redirect(url_for("aws_connections.list_accounts"))


@aws_connections_bp.route("/accounts/<int:connection_id>")
@login_required
def connection_detail(connection_id):
    connection = _own_connection(connection_id)
    return render_template(
        "aws/connection_detail.html",
        connection=connection,
        bucket_form=BucketForm(),
        cfn_command=_cloudformation_command(connection),
        platform_account_id=current_app.config["CLOUDSHIELD_AWS_ACCOUNT_ID"],
        template_name=TEMPLATE_NAME,
    )


@aws_connections_bp.route("/template")
@login_required
def download_template():
    path = (
        Path(current_app.root_path).parent / "infrastructure" / "cloudformation" / TEMPLATE_NAME
    )
    return send_file(path, as_attachment=True, download_name=TEMPLATE_NAME, mimetype="text/yaml")


@aws_connections_bp.route("/accounts/<int:connection_id>/verify", methods=["POST"])
@login_required
def verify_account(connection_id):
    connection = _own_connection(connection_id)
    success, message = verify_connection(connection)
    audit("AWS_CONNECTION_VERIFIED" if success else "AWS_CONNECTION_VERIFY_FAILED",
          "aws_connection", connection.id)
    db.session.commit()
    flash(message, "success" if success else "danger")
    return redirect(
        url_for("aws_connections.connection_detail", connection_id=connection.id)
    )


@aws_connections_bp.route("/accounts/<int:connection_id>/bucket", methods=["POST"])
@login_required
def set_backup_bucket(connection_id):
    connection = _own_connection(connection_id)
    form = BucketForm()
    if form.validate_on_submit():
        bucket = form.backup_bucket.data.strip().lower()
        if not is_valid_bucket_name(bucket):
            flash("Invalid bucket name. Use 3-63 lowercase letters, digits, dots or hyphens.", "danger")
        elif bucket != connection.backup_bucket:
            connection.backup_bucket = bucket
            connection.status = "PENDING"
            connection.status_message = "Backup bucket changed. Verify the connection again."
            audit("BACKUP_BUCKET_CHANGED", "aws_connection", connection.id)
            db.session.commit()
            flash("Backup bucket saved. Verify the connection to confirm access.", "success")
    else:
        flash("Enter a bucket name.", "danger")
    return redirect(
        url_for("aws_connections.connection_detail", connection_id=connection.id)
    )


@aws_connections_bp.route("/accounts/<int:connection_id>/disconnect", methods=["POST"])
@login_required
def disconnect_account(connection_id):
    connection = _own_connection(connection_id)
    audit("AWS_CONNECTION_REMOVED", "aws_connection", connection.id,
          details=f"account {connection.aws_account_id}")
    db.session.delete(connection)
    db.session.commit()
    flash(
        "AWS connection removed from CloudShield. Your backup bucket and its data were NOT "
        "deleted. Delete the CloudFormation stack to revoke access.",
        "warning",
    )
    return redirect(url_for("aws_connections.list_accounts"))
