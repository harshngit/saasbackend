"""Push notifications (FCM): explicit service-account configuration, device registration
and ownership, payload construction, and send / invalid-token handling.

Firebase is never contacted: the service-account key is generated per test run and every
messaging call is mocked."""

import base64
import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core import firebase
from app.core.database import Base, engine
from app.core.security import create_access_token
from app.main import app
from app.models import Organization, PushDevice, User
from app.services import push_service


# ------------------------------------------------------------------ fixtures


def _fake_service_account(project_id: str = "crm-push-test") -> dict:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    return {
        "type": "service_account", "project_id": project_id, "private_key_id": "test-key-id",
        "private_key": pem, "client_email": f"firebase-adminsdk@{project_id}.iam.gserviceaccount.com",
        "client_id": "1234567890", "token_uri": "https://oauth2.googleapis.com/token",
    }


@pytest.fixture(scope="module")
def service_account() -> dict:
    return _fake_service_account()


@pytest.fixture
def push_enabled(monkeypatch, service_account):
    monkeypatch.setattr(firebase.settings, "push_notifications_enabled", True)
    monkeypatch.setattr(firebase.settings, "firebase_service_account_json", json.dumps(service_account))
    monkeypatch.setattr(firebase.settings, "firebase_project_id", "")
    firebase.reset_for_tests()
    yield
    firebase.reset_for_tests()


@pytest.fixture
def push_disabled(monkeypatch):
    monkeypatch.setattr(firebase.settings, "push_notifications_enabled", False)
    monkeypatch.setattr(firebase.settings, "firebase_service_account_json", "")
    firebase.reset_for_tests()
    yield
    firebase.reset_for_tests()


@pytest.fixture
def db_session():
    Base.metadata.create_all(bind=engine)
    session = Session(bind=engine)
    try:
        yield session
    finally:
        session.close()


def _user(db: Session, org: Organization | None = None) -> tuple[User, TestClient]:
    if org is None:
        org = Organization(id=str(uuid.uuid4()), name=f"Push Org {uuid.uuid4().hex[:6]}", company_code="CMP-10001")
        db.add(org)
        db.flush()
    user = User(id=str(uuid.uuid4()), organization_id=org.id, email=f"push_{uuid.uuid4().hex[:8]}@x.com",
                name="Push User", password_hash="h", role="admin", system_role="admin", is_active=True)
    db.add(user)
    db.commit()
    client = TestClient(app)
    client.headers = {"Authorization": f"Bearer {create_access_token(user.id, role='admin', organization_id=org.id)}"}
    return user, client


def _token(tag: str = "") -> str:
    return f"fcm-test-token-{tag}-{uuid.uuid4().hex}{uuid.uuid4().hex}"


def _batch(*outcomes):
    """A send_each() response: each outcome is a message id (success) or an exception."""
    return SimpleNamespace(responses=[
        SimpleNamespace(success=True, message_id=o, exception=None) if isinstance(o, str)
        else SimpleNamespace(success=False, message_id=None, exception=o)
        for o in outcomes
    ])


# ------------------------------------------------------------------ configuration


def test_parse_service_account_validation(service_account):
    parse = firebase.parse_service_account
    raw = json.dumps(service_account)
    assert parse(raw)["project_id"] == "crm-push-test"
    assert parse(base64.b64encode(raw.encode()).decode())["project_id"] == "crm-push-test"  # base64 form

    escaped = {**service_account, "private_key": service_account["private_key"].replace("\n", "\\n")}
    assert "\n" in parse(json.dumps(escaped))["private_key"]  # single-line secret normalised

    bad_cases = {
        "": "not set",
        "{not json": "neither valid JSON",
        json.dumps(["a"]): "JSON object",
        json.dumps({**service_account, "private_key": ""}): "missing: private_key",
        json.dumps({**service_account, "type": "authorized_user"}): "service-account key",
        json.dumps({**service_account, "private_key": "abc"}): "PEM private key",
    }
    for value, message in bad_cases.items():
        with pytest.raises(firebase.FirebaseConfigError, match=message) as exc:
            parse(value)
        assert "PRIVATE KEY" not in str(exc.value)  # never echoes key material

    with pytest.raises(firebase.FirebaseConfigError, match="does not match"):
        parse(raw, expected_project_id="some-other-project")
    assert parse(raw, expected_project_id="crm-push-test")["project_id"] == "crm-push-test"


def test_disabled_and_misconfigured_states(push_disabled, monkeypatch):
    assert firebase.push_status() == {"enabled": False, "configured": False, "project_id": None, "error": None}
    with pytest.raises(firebase.FirebaseConfigError, match="disabled"):
        firebase.get_app()

    monkeypatch.setattr(firebase.settings, "push_notifications_enabled", True)
    status_ = firebase.push_status()
    assert status_["enabled"] and not status_["configured"] and "not set" in status_["error"]
    with pytest.raises(firebase.FirebaseConfigError, match="not set"):
        firebase.get_app()


def test_explicit_certificate_initialisation_without_adc(push_enabled, monkeypatch):
    import firebase_admin
    from firebase_admin import credentials

    def no_adc(*_a, **_k):
        raise AssertionError("Application Default Credentials must never be used")

    monkeypatch.setattr(credentials, "ApplicationDefault", no_adc)
    calls = []
    original = firebase_admin.initialize_app

    def spy(credential=None, options=None, name=firebase_admin._DEFAULT_APP_NAME):
        calls.append((credential, options, name))
        return original(credential, options, name)

    monkeypatch.setattr(firebase_admin, "initialize_app", spy)
    app_a = firebase.get_app()
    app_b = firebase.get_app()  # repeated use: same app, initialised once
    assert app_a is app_b and len(calls) == 1
    credential, options, name = calls[0]
    assert isinstance(credential, credentials.Certificate)
    assert options == {"projectId": "crm-push-test"} and name == firebase.APP_NAME

    # A cleared module cache (e.g. a reload) reuses the existing named app instead of failing
    firebase._app = None
    assert firebase.get_app() is app_a and len(calls) == 1
    assert firebase.push_status()["configured"] is True


def test_unloadable_key_reports_a_config_error(monkeypatch, service_account):
    broken = {**service_account, "private_key": "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n"}
    monkeypatch.setattr(firebase.settings, "push_notifications_enabled", True)
    monkeypatch.setattr(firebase.settings, "firebase_service_account_json", json.dumps(broken))
    firebase.reset_for_tests()
    try:
        with pytest.raises(firebase.FirebaseConfigError, match="could not be loaded") as exc:
            firebase.get_app()
        assert "AAAA" not in str(exc.value)
    finally:
        firebase.reset_for_tests()


# ------------------------------------------------------------------ device API


def test_register_refresh_and_multiple_devices(db_session):
    user, client = _user(db_session)
    web_token, android_token = _token("web"), _token("android")

    first = client.post("/notifications/devices/register", json={"token": web_token, "platform": "web", "label": "Chrome"})
    assert first.status_code == 201, first.text
    body = first.json()
    assert body["platform"] == "web" and body["label"] == "Chrome"
    assert body["token_suffix"] == web_token[-6:] and "token" not in body  # full token never echoed

    again = client.post("/notifications/devices/register", json={"token": f"  {web_token} ", "platform": "web"})
    assert again.status_code == 200 and again.json()["id"] == body["id"]  # idempotent refresh
    assert again.json()["label"] == "Chrome"  # label kept when not resent

    android = client.post("/notifications/devices/register", json={"token": android_token, "platform": "android"})
    assert android.status_code == 201 and android.json()["id"] != body["id"]
    db_session.expire_all()
    assert db_session.query(PushDevice).filter(PushDevice.user_id == user.id).count() == 2


@pytest.mark.parametrize("payload", [
    {"token": "x" * 40, "platform": "blackberry"},
    {"token": "x" * 40, "platform": "WEB"},
    {"token": "short", "platform": "web"},
    {"token": "x" * 1025, "platform": "web"},
    {"token": "has space " + "x" * 30, "platform": "web"},
    {"platform": "web"},
])
def test_register_validation(db_session, payload):
    _, client = _user(db_session)
    assert client.post("/notifications/devices/register", json=payload).status_code == 422


def test_device_endpoints_require_authentication():
    anon = TestClient(app)
    assert anon.post("/notifications/devices/register", json={"token": _token(), "platform": "web"}).status_code in (401, 403)
    assert anon.delete(f"/notifications/devices/{uuid.uuid4()}").status_code in (401, 403)
    assert anon.post("/notifications/devices/test").status_code in (401, 403)


def test_ownership_isolation_and_delete(db_session):
    owner, owner_client = _user(db_session)
    org = db_session.get(Organization, owner.organization_id)
    colleague, colleague_client = _user(db_session, org)   # same firm, different user
    outsider, outsider_client = _user(db_session)            # different firm

    device_id = owner_client.post("/notifications/devices/register",
                                  json={"token": _token("own"), "platform": "web"}).json()["id"]
    assert colleague_client.delete(f"/notifications/devices/{device_id}").status_code == 404
    assert outsider_client.delete(f"/notifications/devices/{device_id}").status_code == 404
    db_session.expire_all()
    assert db_session.get(PushDevice, device_id) is not None

    assert owner_client.delete(f"/notifications/devices/{device_id}").status_code == 204
    assert owner_client.delete(f"/notifications/devices/{device_id}").status_code == 404  # repeat
    db_session.expire_all()
    assert db_session.get(PushDevice, device_id) is None


def test_reregistering_a_token_in_the_same_firm_moves_it_to_the_current_user(db_session):
    first_user, first_client = _user(db_session)
    org = db_session.get(Organization, first_user.organization_id)
    second_user, second_client = _user(db_session, org)  # same firm, same physical device
    token = _token("shared")
    first_id = first_client.post("/notifications/devices/register", json={"token": token, "platform": "android"}).json()["id"]
    res = second_client.post("/notifications/devices/register", json={"token": token, "platform": "android"})
    assert res.status_code == 200 and res.json()["id"] == first_id
    db_session.expire_all()
    device = db_session.get(PushDevice, first_id)
    assert (device.user_id, device.organization_id) == (second_user.id, org.id)
    assert push_service.devices_for_user(db_session, first_user) == []  # old owner no longer reached
    assert first_client.delete(f"/notifications/devices/{first_id}").status_code == 404


def test_token_from_another_firm_is_rejected_and_left_unchanged(db_session):
    owner, owner_client = _user(db_session)
    other, other_client = _user(db_session)  # different firm
    token = _token("crossfirm")
    first = owner_client.post("/notifications/devices/register", json={"token": token, "platform": "web", "label": "Office PC"})
    device_id = first.json()["id"]

    res = other_client.post("/notifications/devices/register", json={"token": token, "platform": "android", "label": "Mine"})
    assert res.status_code == 409
    assert token not in res.text and "registered to another organization" in res.json()["detail"]
    db_session.expire_all()
    rows = db_session.query(PushDevice).filter(PushDevice.token == token).all()
    assert len(rows) == 1
    assert (rows[0].id, rows[0].user_id, rows[0].organization_id) == (device_id, owner.id, owner.organization_id)
    assert (rows[0].platform, rows[0].label) == ("web", "Office PC")  # nothing on the row changed
    assert push_service.devices_for_user(db_session, other) == []

    # The documented path: the previous user unregisters the device, then the new firm can register it
    assert owner_client.delete(f"/notifications/devices/{device_id}").status_code == 204
    again = other_client.post("/notifications/devices/register", json={"token": token, "platform": "android"})
    assert again.status_code == 201
    db_session.expire_all()
    assert db_session.get(PushDevice, again.json()["id"]).organization_id == other.organization_id


def test_device_cap_per_user(db_session):
    user, client = _user(db_session)
    for i in range(push_service.MAX_DEVICES_PER_USER + 3):
        assert client.post("/notifications/devices/register",
                           json={"token": _token(f"cap{i}"), "platform": "web"}).status_code == 201
    db_session.expire_all()
    assert db_session.query(PushDevice).filter(PushDevice.user_id == user.id).count() == push_service.MAX_DEVICES_PER_USER


# ------------------------------------------------------------------ payloads


def test_payload_construction_for_web_and_android():
    web = SimpleNamespace(token=_token("w"), platform="web")
    android = SimpleNamespace(token=_token("a"), platform="android")
    data = {"type": "order", "id": "SO-1", "count": 3, "urgent": True, "skip": None}

    msg = push_service.build_message(android, "New order", "SO-1 placed", data)
    assert msg.token == android.token and msg.notification.title == "New order"
    assert msg.android.priority == "high" and msg.webpush is None
    assert msg.data == {"type": "order", "id": "SO-1", "count": "3", "urgent": "true"}

    msg = push_service.build_message(web, "New order", None, data, link="https://crm.example.com/orders/1")
    assert msg.webpush.fcm_options.link == "https://crm.example.com/orders/1" and msg.android is None
    assert push_service.build_message(web, "T", link="http://insecure").webpush.fcm_options is None
    assert push_service.build_message(web, "T").data is None

    for bad in ({"from": "x"}, {"google.x": "y"}, {"gcm.z": "y"}, {"k": {"nested": 1}}, {"k": "v" * 4000}):
        with pytest.raises(push_service.PushPayloadError):
            push_service.build_message(web, "T", data=bad)
    with pytest.raises(push_service.PushPayloadError):
        push_service.build_message(web, "  ")


# ------------------------------------------------------------------ sending


def _devices(n: int, platform: str = "web"):
    return [SimpleNamespace(id=str(i), token=_token(str(i)), platform=platform) for i in range(n)]


def test_successful_send_counts_accepted_not_delivered(push_enabled, monkeypatch):
    from firebase_admin import messaging

    sent = []
    monkeypatch.setattr(messaging, "send_each", lambda msgs, app=None: sent.append((msgs, app)) or _batch("m1", "m2"))
    db = MagicMock()
    result = push_service.send_to_devices(db, _devices(2), "Hello", "World", {"type": "info"})
    assert (result.attempted, result.accepted, result.invalid_tokens_removed, result.transient_failures) == (2, 2, 0, 0)
    assert result.message_ids == ["m1", "m2"]
    assert sent[0][1] is firebase.get_app()  # the explicit named app, not the default app
    db.delete.assert_not_called()


def test_invalid_tokens_removed_transient_failures_kept(push_enabled, monkeypatch):
    from firebase_admin import exceptions, messaging

    outcomes = _batch(
        "ok",
        messaging.UnregisteredError("Requested entity was not found."),
        messaging.SenderIdMismatchError("SenderId mismatch"),
        exceptions.InvalidArgumentError("The registration token is not a valid FCM registration token"),
        exceptions.UnavailableError("FCM unavailable"),
        messaging.QuotaExceededError("quota"),
        exceptions.InvalidArgumentError("Invalid JSON payload received."),  # payload problem, not the token
    )
    monkeypatch.setattr(messaging, "send_each", lambda msgs, app=None: outcomes)
    devices = _devices(7)
    db = MagicMock()
    result = push_service.send_to_devices(db, devices, "Hello")
    assert (result.accepted, result.invalid_tokens_removed, result.transient_failures) == (1, 3, 3)
    assert [c.args[0] for c in db.delete.call_args_list] == devices[1:4]


def test_whole_batch_failure_keeps_every_token(push_enabled, monkeypatch):
    from firebase_admin import exceptions, messaging

    def boom(msgs, app=None):
        raise exceptions.UnavailableError("network down")

    monkeypatch.setattr(messaging, "send_each", boom)
    db = MagicMock()
    result = push_service.send_to_devices(db, _devices(3), "Hello")
    assert (result.accepted, result.transient_failures, result.invalid_tokens_removed) == (0, 3, 0)
    db.delete.assert_not_called()


def test_batches_respect_the_500_message_limit(push_enabled, monkeypatch):
    from firebase_admin import messaging

    sizes = []
    monkeypatch.setattr(messaging, "send_each",
                        lambda msgs, app=None: sizes.append(len(msgs)) or _batch(*["id"] * len(msgs)))
    result = push_service.send_to_devices(MagicMock(), _devices(501, "android"), "Hello")
    assert sizes == [500, 1] and result.accepted == 501


def test_send_when_disabled_or_no_devices(push_disabled, monkeypatch):
    from firebase_admin import messaging

    monkeypatch.setattr(messaging, "send_each", lambda *a, **k: pytest.fail("must not call Firebase"))
    result = push_service.send_to_devices(MagicMock(), _devices(2), "Hello")
    assert result.enabled is False and result.attempted == 0 and "disabled" in result.detail
    empty = push_service.send_to_devices(MagicMock(), [], "Hello")
    assert empty.enabled is False and "disabled" in empty.detail  # config state reported first


def test_send_with_no_devices_when_enabled(push_enabled):
    result = push_service.send_to_devices(MagicMock(), [], "Hello")
    assert result.enabled is True and result.attempted == 0 and result.detail == "no registered devices"


def test_test_endpoint_reaches_only_the_callers_devices(db_session, push_enabled, monkeypatch):
    from firebase_admin import messaging

    user, client = _user(db_session)
    other, other_client = _user(db_session)
    mine = _token("mine")
    client.post("/notifications/devices/register", json={"token": mine, "platform": "web"})
    other_client.post("/notifications/devices/register", json={"token": _token("theirs"), "platform": "android"})

    seen = []

    def fake_send(msgs, app=None):
        seen.extend(m.token for m in msgs)
        return _batch(messaging.UnregisteredError("gone"))

    monkeypatch.setattr(messaging, "send_each", fake_send)
    res = client.post("/notifications/devices/test")
    assert res.status_code == 200, res.text
    assert seen == [mine]  # never another user's device
    assert res.json() == {"enabled": True, "attempted": 1, "accepted": 0, "invalid_tokens_removed": 1,
                          "transient_failures": 0, "detail": None}
    db_session.expire_all()
    assert push_service.devices_for_user(db_session, user) == []  # dead token removed and committed


def test_existing_apis_unaffected_when_push_off_or_misconfigured(db_session, push_disabled, monkeypatch):
    _, client = _user(db_session)
    assert client.get("/notifications").status_code == 200
    assert client.post("/notifications/devices/test").json()["enabled"] is False
    monkeypatch.setattr(firebase.settings, "push_notifications_enabled", True)
    monkeypatch.setattr(firebase.settings, "firebase_service_account_json", "{broken")
    assert client.get("/notifications").status_code == 200
    assert client.post("/notifications/devices/register", json={"token": _token(), "platform": "web"}).status_code == 201
    res = client.post("/notifications/devices/test").json()
    assert res["enabled"] is False and "neither valid JSON" in res["detail"]


# ------------------------------------------------------------------ hardening


def _lose_the_race(monkeypatch):
    """Make the first token lookup miss, as if a concurrent request inserted the same token
    between this request's lookup and its insert."""
    real = push_service._device_for_token
    calls = {"n": 0}

    def lookup(db, token):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(db, token)

    monkeypatch.setattr(push_service, "_device_for_token", lookup)
    return calls


@pytest.mark.parametrize("same_user", [True, False])
def test_concurrent_registration_of_the_same_token(db_session, monkeypatch, same_user):
    winner, winner_client = _user(db_session)
    org = db_session.get(Organization, winner.organization_id)
    loser, loser_client = (winner, winner_client) if same_user else _user(db_session, org)  # same firm
    token = _token("race")
    won = winner_client.post("/notifications/devices/register", json={"token": token, "platform": "web"})
    assert won.status_code == 201

    calls = _lose_the_race(monkeypatch)
    res = loser_client.post("/notifications/devices/register", json={"token": token, "platform": "android"})
    assert calls["n"] == 2  # the insert hit the unique constraint and the row was re-read
    assert res.status_code == 200 and res.json()["id"] == won.json()["id"]
    db_session.expire_all()
    rows = db_session.query(PushDevice).filter(PushDevice.token == token).all()
    assert len(rows) == 1  # still exactly one, valid registration
    assert (rows[0].user_id, rows[0].platform) == (loser.id, "android")


def test_lost_race_against_another_firm_is_rejected(db_session, monkeypatch):
    winner, winner_client = _user(db_session)
    _, loser_client = _user(db_session)  # different firm
    token = _token("racefirm")
    won = winner_client.post("/notifications/devices/register", json={"token": token, "platform": "web"})

    calls = _lose_the_race(monkeypatch)
    res = loser_client.post("/notifications/devices/register", json={"token": token, "platform": "android"})
    assert calls["n"] == 2 and res.status_code == 409  # re-read after the unique violation, then refused
    db_session.expire_all()
    rows = db_session.query(PushDevice).filter(PushDevice.token == token).all()
    assert len(rows) == 1
    assert (rows[0].id, rows[0].user_id, rows[0].platform) == (won.json()["id"], winner.id, "web")


def test_unrelated_integrity_errors_are_not_swallowed(db_session):
    from sqlalchemy.exc import IntegrityError

    ghost = SimpleNamespace(id=str(uuid.uuid4()), organization_id=None)  # no such user: FK violation
    with pytest.raises(IntegrityError):
        push_service.register_device(db_session, ghost, _token("ghost"), "web", None)
    db_session.rollback()


def test_enabled_but_firebase_sdk_missing(db_session, push_enabled, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "firebase_admin", None)  # import firebase_admin -> ImportError
    with pytest.raises(firebase.FirebaseConfigError, match="firebase-admin package is not installed") as exc:
        firebase.get_app()
    assert "PRIVATE KEY" not in str(exc.value)

    _, client = _user(db_session)
    client.post("/notifications/devices/register", json={"token": _token("nosdk"), "platform": "web"})
    res = client.post("/notifications/devices/test")
    assert res.status_code == 200  # controlled, not an unhandled 500
    body = res.json()
    assert body["enabled"] is False and body["attempted"] == 0 and "not installed" in body["detail"]


def test_disabled_takes_precedence_over_missing_sdk(push_disabled, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "firebase_admin", None)
    with pytest.raises(firebase.FirebaseConfigError, match="disabled"):
        firebase.get_app()


# ------------------------------------------------------------------ migration


def test_push_devices_migration_up_and_down(tmp_path):
    import importlib.util
    import pathlib

    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = pathlib.Path(__file__).resolve().parents[1] / "alembic" / "versions" / "b6c7d8e9f0a1_push_devices.py"
    spec = importlib.util.spec_from_file_location("push_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    mig_engine = sa.create_engine(f"sqlite:///{tmp_path / 'push.db'}")
    Base.metadata.create_all(mig_engine, tables=[t for t in Base.metadata.sorted_tables if t.name != "push_devices"])

    def run(step):
        with mig_engine.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                step()

    run(migration.upgrade)
    run(migration.upgrade)  # idempotent
    inspector = sa.inspect(mig_engine)
    assert "push_devices" in inspector.get_table_names()
    assert {ix["name"] for ix in inspector.get_indexes("push_devices")} >= {"ix_push_devices_user_id", "ix_push_devices_organization_id"}
    assert any(u["column_names"] == ["token"] for u in inspector.get_unique_constraints("push_devices"))
    run(migration.downgrade)
    assert "push_devices" not in sa.inspect(mig_engine).get_table_names()
    mig_engine.dispose()


# ------------------------------------------------------------------ iOS and the payload contract


def test_ios_registration_and_apns_payload(db_session):
    _, client = _user(db_session)
    res = client.post("/notifications/devices/register", json={"token": _token("i"), "platform": "ios"})
    assert res.status_code == 201 and res.json()["platform"] == "ios"

    ios = SimpleNamespace(token=_token("i"), platform="ios")
    msg = push_service.build_message(ios, "Order completed", "SO-1", {"type": "order_status_changed"})
    assert msg.android is None and msg.webpush is None
    assert msg.apns.payload.aps.alert.title == "Order completed" and msg.apns.payload.aps.alert.body == "SO-1"
    assert msg.apns.payload.aps.sound == "default"
    # Web and Android carry no APNs block.
    for platform in ("web", "android"):
        assert push_service.build_message(SimpleNamespace(token=_token(), platform=platform), "T").apns is None


def test_build_data_contract():
    from app.core import notification_types as nt

    assert nt.build_data(nt.ORDER_STATUS_CHANGED, order_id=12, status="completed") == {
        "event": "order_status_changed", "order_id": "12", "status": "completed"}
    assert nt.build_data(nt.PAYMENT_RECEIVED, payment_id="p", invoice_id=None) == {
        "event": "payment_received", "payment_id": "p"}
    with pytest.raises(ValueError):
        nt.build_data("not_an_event")
    with pytest.raises(ValueError):
        nt.build_data(nt.ORDER_CREATED, order_id="o", customer_phone="98765")
    assert set(nt.EVENT_CATEGORY) == set(nt.ALL_EVENTS)


# ------------------------------------------------------------------ in-app fields


def _member(db: Session, org_id: str, role: str = "sales_officer", system_role: str | None = "staff",
            active: bool = True) -> User:
    user = User(id=str(uuid.uuid4()), organization_id=org_id, email=f"m_{uuid.uuid4().hex[:8]}@x.com",
                name="Member", password_hash="h", role=role, system_role=system_role, is_active=active)
    db.add(user)
    db.commit()
    return user


def test_notification_data_read_at_and_read_all(db_session):
    from app.core import notification_types as nt
    from app.models import Notification
    from app.services import notification_service

    user, client = _user(db_session)
    n = notification_service.notify(db_session, user.id, "Leave approved",
                                    data=nt.build_data(nt.LEAVE_STATUS_CHANGED, leave_id="L1", status="approved"))
    notification_service.notify(db_session, user.id, "Second", event=nt.PUSH_TEST)
    db_session.commit()
    assert n.type == "leave" and n.organization_id == user.organization_id and n.read_at is None

    listed = client.get("/notifications").json()
    items = listed["items"] if isinstance(listed, dict) else listed
    row = next(i for i in items if i["id"] == n.id)
    assert row["data"] == {"event": "leave_status_changed", "leave_id": "L1", "status": "approved"}
    assert row["read_at"] is None

    assert client.patch(f"/notifications/{n.id}/read").status_code == 200
    db_session.expire_all()
    stamped = db_session.get(Notification, n.id).read_at
    assert stamped is not None
    client.patch(f"/notifications/{n.id}/read")
    db_session.expire_all()
    assert db_session.get(Notification, n.id).read_at == stamped  # first read wins

    assert client.post("/notifications/read-all").status_code == 200
    assert client.patch("/notifications/read-all").status_code == 200  # idempotent, both verbs
    db_session.expire_all()
    rows = db_session.query(Notification).filter(Notification.user_id == user.id).all()
    assert len(rows) == 2 and all(r.is_read and r.read_at is not None for r in rows)
    assert db_session.get(Notification, n.id).read_at == stamped


def test_org_admins_uses_the_effective_role(db_session):
    from app.services import notification_service

    owner, _ = _user(db_session)
    org_id = owner.organization_id
    legacy_admin = _member(db_session, org_id, role="admin", system_role=None)
    demoted = _member(db_session, org_id, role="admin", system_role="staff")  # legacy role still says admin
    promoted = _member(db_session, org_id, role="accountant", system_role="admin")
    _member(db_session, org_id, role="admin", system_role="admin", active=False)
    _member(db_session, org_id)
    other_org_admin, _ = _user(db_session)

    ids = {u.id for u in notification_service.org_admins(db_session, org_id)}
    assert ids == {owner.id, legacy_admin.id, promoted.id}
    assert demoted.id not in ids and other_org_admin.id not in ids

    created = notification_service.notify_org_admins(db_session, org_id, "Hi", exclude_user_id=owner.id)
    assert {n.user_id for n in created} == {legacy_admin.id, promoted.id}
    db_session.rollback()


# ------------------------------------------------------------------ push after commit


@pytest.fixture
def inline_push(monkeypatch):
    """Push enabled, the worker pool replaced by an inline call, and send_to_user recorded."""
    monkeypatch.setattr(push_service.settings, "push_notifications_enabled", True)
    sent = []

    def fake_send(db, user, title, body=None, data=None, link=None):
        sent.append({"user_id": user.id, "title": title, "data": data})
        return push_service.PushResult(enabled=True, attempted=1, accepted=1)

    monkeypatch.setattr(push_service, "send_to_user", fake_send)
    monkeypatch.setattr(push_service, "_dispatch", push_service.deliver_notification_pushes)
    return sent


def test_push_is_sent_only_after_commit_with_the_same_id_and_type(db_session, inline_push):
    from app.core import notification_types as nt
    from app.services import notification_service

    user, _ = _user(db_session)
    n = notification_service.notify(db_session, user.id, "Order completed", "SO-1",
                                    data=nt.build_data(nt.ORDER_STATUS_CHANGED, order_id="o1", status="completed"))
    db_session.flush()
    assert inline_push == []  # nothing before commit
    db_session.commit()
    assert len(inline_push) == 1
    data = inline_push[0]["data"]
    assert data["notification_id"] == n.id and data["type"] == "order_status_changed"
    assert data["category"] == "order" and data["order_id"] == "o1" and data["status"] == "completed"
    assert all(isinstance(v, str) for v in data.values())

    db_session.commit()  # a later commit does not resend
    assert len(inline_push) == 1


def test_rollback_discards_the_push(db_session, inline_push):
    from app.models import Notification
    from app.services import notification_service

    user, _ = _user(db_session)
    n = notification_service.notify(db_session, user.id, "Rolled back")
    db_session.rollback()
    db_session.commit()
    assert inline_push == [] and db_session.get(Notification, n.id) is None


def test_push_failure_keeps_the_in_app_notification(db_session, inline_push, monkeypatch):
    from app.models import Notification
    from app.services import notification_service

    def boom(*a, **k):
        raise RuntimeError("firebase down")

    monkeypatch.setattr(push_service, "send_to_user", boom)
    user, _ = _user(db_session)
    n = notification_service.notify(db_session, user.id, "Still here")
    db_session.commit()  # must not raise
    db_session.expire_all()
    assert db_session.get(Notification, n.id) is not None


def test_push_disabled_still_creates_in_app_notifications(db_session, monkeypatch):
    from app.models import Notification
    from app.services import notification_service

    monkeypatch.setattr(push_service.settings, "push_notifications_enabled", False)
    monkeypatch.setattr(push_service, "_dispatch", lambda ids: pytest.fail("must not push"))
    user, _ = _user(db_session)
    n = notification_service.notify(db_session, user.id, "In-app only")
    db_session.commit()
    assert db_session.get(Notification, n.id) is not None


# ------------------------------------------------------------------ business events


def test_order_status_changed_recipients_and_guards(db_session, inline_push):
    from app.services import notification_service

    owner, _ = _user(db_session)
    other_admin = _member(db_session, owner.organization_id, role="admin", system_role="admin")
    order = SimpleNamespace(id="o1", organization_id=owner.organization_id, order_number="SO-9", status="completed")

    created = notification_service.notify_order_status_changed(db_session, order, "processing", actor_id=owner.id)
    assert [n.user_id for n in created] == [other_admin.id]
    assert created[0].data == {"event": "order_status_changed", "order_id": "o1", "status": "completed"}
    assert notification_service.notify_order_status_changed(db_session, order, "completed") == []  # unchanged
    order.status = "processing"
    assert notification_service.notify_order_status_changed(db_session, order, "placed") == []  # not notified
    db_session.commit()
    assert [p["user_id"] for p in inline_push] == [other_admin.id]


def test_delivery_status_changed_recipients_and_guards(db_session):
    from app.services import notification_service

    owner, _ = _user(db_session)
    partner = _member(db_session, owner.organization_id, role="delivery_partner")
    delivery = SimpleNamespace(id="d1", organization_id=owner.organization_id, delivery_number="DL-1",
                               delivery_note_number=None, delivery_partner_id=partner.id, status="delivered")

    created = notification_service.notify_delivery_status_changed(db_session, delivery, "in_transit", actor_id=partner.id)
    assert [n.user_id for n in created] == [owner.id]
    assert created[0].type == "delivery" and created[0].link == "d1"
    delivery.status = "failed"
    assert notification_service.notify_delivery_status_changed(db_session, delivery, "failed") == []

    delivery.status = "cancelled"
    created = notification_service.notify_delivery_status_changed(db_session, delivery, "planned", actor_id=owner.id)
    assert [n.user_id for n in created] == [partner.id]
    assert created[0].data == {"event": "delivery_status_changed", "delivery_id": "d1", "status": "cancelled"}
    assert notification_service.notify_delivery_status_changed(db_session, delivery, "planned", actor_id=partner.id) == []
    delivery.delivery_partner_id = None
    assert notification_service.notify_delivery_status_changed(db_session, delivery, "planned") == []
    db_session.rollback()


def test_leave_decision_notifies_the_applicant_once(db_session, monkeypatch):
    from datetime import date

    from app.models import Leave, Notification
    from app.services import entitlement_service

    # The test firm has no plan; the leave module's plan gate is not what is under test.
    monkeypatch.setattr(entitlement_service, "check_entitlement", lambda *a, **k: None)

    admin, client = _user(db_session)
    staff = _member(db_session, admin.organization_id)
    leave = Leave(id=str(uuid.uuid4()), organization_id=admin.organization_id, user_id=staff.id,
                  start_date=date(2026, 11, 2), end_date=date(2026, 11, 3), days_count=2)
    db_session.add(leave)
    db_session.commit()

    assert client.patch(f"/leaves/{leave.id}/approve").status_code == 200
    assert client.patch(f"/leaves/{leave.id}/approve").status_code == 400  # no longer pending
    db_session.expire_all()
    rows = db_session.query(Notification).filter(Notification.user_id == staff.id).all()
    assert len(rows) == 1 and rows[0].type == "leave"
    assert rows[0].data == {"event": "leave_status_changed", "leave_id": leave.id, "status": "approved"}
    assert db_session.query(Notification).filter(Notification.user_id == admin.id).count() == 0


def test_manual_payment_receipt_notifies_other_admins(db_session):
    from app.models import Customer, Notification

    recorder, client = _user(db_session)
    other_admin = _member(db_session, recorder.organization_id, role="admin", system_role="admin")
    customer = Customer(id=str(uuid.uuid4()), organization_id=recorder.organization_id, name="Cust")
    db_session.add(customer)
    db_session.commit()

    res = client.post("/payment-receipts", json={"customer_id": customer.id, "amount_received": 100,
                                                 "payment_method": "cash"})
    assert res.status_code in (200, 201), res.text
    db_session.expire_all()
    rows = db_session.query(Notification).filter(
        Notification.organization_id == recorder.organization_id, Notification.title == "Payment received").all()
    assert [r.user_id for r in rows] == [other_admin.id]
    assert rows[0].data == {"event": "payment_received", "payment_id": res.json()["id"]}


def test_notifications_migration_up_and_down(tmp_path):
    import importlib.util
    import pathlib

    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = (pathlib.Path(__file__).resolve().parents[1] / "alembic" / "versions"
            / "c7d8e9f0a1b2_notifications_data_read_at.py")
    spec = importlib.util.spec_from_file_location("notifications_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    index_names = {name for name, _ in migration._INDEXES}

    mig_engine = sa.create_engine(f"sqlite:///{tmp_path / 'n.db'}")
    with mig_engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE notifications (id VARCHAR(36) PRIMARY KEY, user_id VARCHAR(36), "
                             "organization_id VARCHAR(36), title VARCHAR(200), is_read BOOLEAN, created_at DATETIME)"))
        conn.execute(sa.text("INSERT INTO notifications (id, user_id, title, is_read) VALUES ('n1', 'u1', 'old', 0)"))

    def run(step):
        with mig_engine.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                step()

    run(migration.upgrade)
    run(migration.upgrade)  # idempotent
    inspector = sa.inspect(mig_engine)
    assert {"data", "read_at"} <= {c["name"] for c in inspector.get_columns("notifications")}
    assert index_names <= {ix["name"] for ix in inspector.get_indexes("notifications")}
    run(migration.downgrade)
    assert not index_names & {ix["name"] for ix in sa.inspect(mig_engine).get_indexes("notifications")}
    with mig_engine.connect() as conn:
        assert conn.execute(sa.text("SELECT title FROM notifications WHERE id = 'n1'")).scalar() == "old"
    mig_engine.dispose()
