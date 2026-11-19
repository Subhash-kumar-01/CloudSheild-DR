from datetime import timedelta

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from flask_wtf import FlaskForm
from wtforms import BooleanField, PasswordField, StringField
from wtforms.validators import DataRequired, Email, EqualTo, Length

from app.extensions import db
from app.models import AuditLog, User
from app.utils import audit, is_safe_next_url, utcnow

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


class RegistrationForm(FlaskForm):
    full_name = StringField("Full name", validators=[DataRequired(), Length(max=120)])
    email = StringField("Email", validators=[DataRequired(), Email(), Length(max=255)])
    password = PasswordField(
        "Password", validators=[DataRequired(), Length(min=12, max=128)]
    )
    confirm_password = PasswordField(
        "Confirm password", validators=[DataRequired(), EqualTo("password")]
    )


class LoginForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email()])
    password = PasswordField("Password", validators=[DataRequired()])
    remember_me = BooleanField("Keep me signed in")


def _is_locked_out(user: User) -> bool:
    """Block password guessing: too many recent failures for this account."""
    since = utcnow() - timedelta(minutes=current_app.config["LOGIN_LOCK_MINUTES"])
    failures = AuditLog.query.filter(
        AuditLog.owner_id == user.id,
        AuditLog.action == "USER_LOGIN_FAILED",
        AuditLog.created_at >= since,
    ).count()
    return failures >= current_app.config["LOGIN_MAX_FAILURES"]


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    form = RegistrationForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        if User.query.filter_by(email=email).first():
            flash("An account with this email already exists.", "danger")
            return render_template("auth/register.html", form=form)

        user = User(email=email, full_name=form.full_name.data.strip())
        user.set_password(form.password.data)
        db.session.add(user)
        db.session.commit()
        audit("USER_REGISTERED", "user", user.id, owner_id=user.id)
        db.session.commit()

        flash("Account created. Please sign in.", "success")
        return redirect(url_for("auth.login"))

    return render_template("auth/register.html", form=form)


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data.strip().lower()).first()

        if user and _is_locked_out(user):
            flash("Too many failed attempts. Try again in a few minutes.", "danger")
            return render_template("auth/login.html", form=form), 429

        if user and user.is_active and user.check_password(form.password.data):
            login_user(user, remember=form.remember_me.data)
            audit("USER_LOGIN", "user", user.id, owner_id=user.id)
            db.session.commit()
            next_url = request.args.get("next", "")
            return redirect(next_url if is_safe_next_url(next_url) else url_for("dashboard.index"))

        if user:
            audit("USER_LOGIN_FAILED", "user", user.id, owner_id=user.id)
            db.session.commit()
        flash("Invalid email or password.", "danger")

    return render_template("auth/login.html", form=form)


@auth_bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))
