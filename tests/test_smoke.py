"""App-level smoke tests. Need the full dependency set: pip install -r requirements-dev.txt"""
import pytest

from app import create_app
from app.extensions import db
from app.models import AwsConnection, User
from app.utils import utcnow


@pytest.fixture()
def app():
    app = create_app("testing")
    with app.app_context():
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


def register_and_login(client, email="owner@example.com"):
    client.post("/auth/register", data={
        "full_name": "Owner", "email": email,
        "password": "a-very-long-password", "confirm_password": "a-very-long-password",
    })
    return client.post("/auth/login", data={"email": email, "password": "a-very-long-password"})


def make_connection(user_email="owner@example.com", status="CONNECTED"):
    user = User.query.filter_by(email=user_email).first()
    connection = AwsConnection(
        owner_id=user.id,
        display_name="Prod",
        aws_account_id="123456789012",
        region="ap-south-1",
        role_arn="arn:aws:iam::123456789012:role/CloudShieldDRBackupRole",
        external_id=AwsConnection.generate_external_id(),
        status=status,
        backup_bucket="cloudshield-dr-test",
        last_verified_at=utcnow(),
    )
    db.session.add(connection)
    db.session.commit()
    return connection


def test_health(client):
    assert client.get("/health").status_code == 200


def test_pages_require_login(client):
    for url in ("/", "/aws/accounts", "/backups/", "/schedules/", "/incidents/", "/backups/recoveries"):
        response = client.get(url)
        assert response.status_code in (302, 401), url


def test_register_login_and_every_page_renders(client):
    response = register_and_login(client)
    assert response.status_code == 302
    make_connection()
    for url in ("/", "/aws/accounts", "/aws/accounts/1", "/backups/", "/schedules/",
                "/incidents/", "/backups/recoveries"):
        assert client.get(url).status_code == 200, url


def test_users_cannot_see_each_others_connections(client):
    register_and_login(client, "a@example.com")
    connection = make_connection("a@example.com")
    client.post("/auth/logout")
    register_and_login(client, "b@example.com")
    assert client.get(f"/aws/accounts/{connection.id}").status_code == 404


def test_disconnect_with_history_does_not_crash(client):
    register_and_login(client)
    connection = make_connection()
    response = client.post(f"/aws/accounts/{connection.id}/disconnect")
    assert response.status_code == 302
    assert AwsConnection.query.count() == 0


def test_open_redirect_is_blocked(client):
    client.post("/auth/register", data={
        "full_name": "O", "email": "o@example.com",
        "password": "a-very-long-password", "confirm_password": "a-very-long-password"})
    response = client.post("/auth/login?next=//evil.example",
                           data={"email": "o@example.com", "password": "a-very-long-password"})
    assert "evil.example" not in response.headers["Location"]
