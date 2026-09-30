"""Organization Theme & Appearance Customization (app/routers/settings.py's
/organization/theme endpoints, app/services/theme_service.py,
app/models/organization_theme.py).

Covers: default-theme-without-a-row behavior, PATCH merge semantics, the
Admin-only permission matrix (Business Owner and Admin are the same
system_role="admin" tier in this codebase — see the "business owner" test
below), tenant isolation, field validation, background/logo uploads
(type/size limits, /files/{id} URLs, never touching Organization.logo_url),
and activity logging. Runs only against the local test database.
"""

import io
import os
import sys
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.database import SessionLocal
from app.main import app
from app.models import ActivityLog, Organization, OrganizationTheme, User

client = TestClient(app)


def _register_org(name_prefix: str) -> tuple[dict, str]:
    """Registers a brand-new organization; the registering user is the
    "Business Owner" — created with system_role="admin" directly, exactly
    like any other Admin (see app/services/auth_service.py)."""
    email = f"{name_prefix}_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={
        "organization_name": f"{name_prefix} {uuid.uuid4().hex[:6]}",
        "admin_name": "Owner",
        "email": email,
        "password": "Password123!",
        "role": "admin",
    })
    assert r.status_code == 201, r.text
    token = r.json()["tokens"]["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    db = SessionLocal()
    try:
        org_id = db.query(User).filter(User.email == email).first().organization_id
    finally:
        db.close()
    return headers, org_id


def _make_staff(owner_headers: dict, role: str) -> dict:
    """Creates a staff user with a real seeded role and logs in as them."""
    email = f"staff_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/users", json={
        "name": "Staff", "email": email, "password": "Password123!", "role": role,
    }, headers=owner_headers)
    assert r.status_code == 201, r.text
    r2 = client.post("/auth/login", json={"email": email, "password": "Password123!"})
    assert r2.status_code == 200, r2.text
    return {"Authorization": f"Bearer {r2.json()['tokens']['access_token']}"}


def _png_bytes() -> bytes:
    # Nothing in this upload path decodes image bytes (see _check_theme_image_type
    # and app.core.files._check_type, both content_type-header-only checks) —
    # arbitrary bytes with the right declared content_type is sufficient and
    # matches how every other upload test in this suite already works.
    return b"fake-png-bytes"


# ------------------------------- 1-2. defaults / no row yet -----------------------


def test_default_theme_behavior():
    headers, _ = _register_org("theme_default")
    r = client.get("/organization/theme", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["custom_enabled"] is False
    assert body["theme"]["theme_name"] == "default"
    assert body["theme"]["mode"] == "light"
    assert body["theme"]["fonts"]["heading"] == "DM Sans"
    assert body["theme"]["fonts"]["body"] == "Open Sans"
    assert body["theme"]["colors"]["primary"] is None
    assert body["theme"]["background"]["url"] is None


def test_get_theme_when_no_row_exists_does_not_create_one():
    headers, org_id = _register_org("theme_no_row")
    client.get("/organization/theme", headers=headers)
    db = SessionLocal()
    try:
        assert db.query(OrganizationTheme).filter(OrganizationTheme.organization_id == org_id).first() is None
    finally:
        db.close()


# ------------------------------- 3-6. PATCH semantics ------------------------------


def test_get_theme_for_existing_organization_after_patch():
    headers, _ = _register_org("theme_existing")
    client.patch("/organization/theme", json={"mode": "dark"}, headers=headers)
    r = client.get("/organization/theme", headers=headers)
    assert r.json()["theme"]["mode"] == "dark"


def test_patch_creates_theme_row_when_missing():
    headers, org_id = _register_org("theme_patch_create")
    r = client.patch("/organization/theme", json={"custom_enabled": True}, headers=headers)
    assert r.status_code == 200, r.text
    db = SessionLocal()
    try:
        assert db.query(OrganizationTheme).filter(OrganizationTheme.organization_id == org_id).first() is not None
    finally:
        db.close()


def test_patch_updates_only_supplied_fields():
    headers, _ = _register_org("theme_partial")
    client.patch("/organization/theme", json={"mode": "dark", "primary_color": "#22c55e"}, headers=headers)
    r = client.patch("/organization/theme", json={"card_style": "glass"}, headers=headers)
    body = r.json()
    assert body["theme"]["card_style"] == "glass"
    assert body["theme"]["mode"] == "dark"  # untouched by the second PATCH
    assert body["theme"]["colors"]["primary"] == "#22c55e"  # untouched


def test_patch_preserves_omitted_fields_across_multiple_updates():
    headers, _ = _register_org("theme_preserve")
    client.patch("/organization/theme", json={"heading_font": "Roboto"}, headers=headers)
    client.patch("/organization/theme", json={"body_font": "Lato"}, headers=headers)
    r = client.get("/organization/theme", headers=headers)
    assert r.json()["theme"]["fonts"]["heading"] == "Roboto"
    assert r.json()["theme"]["fonts"]["body"] == "Lato"


# ------------------------------- 7-8. custom_enabled -------------------------------


def test_custom_enabled_false_still_returns_effective_configuration():
    headers, _ = _register_org("theme_disabled")
    client.patch("/organization/theme", json={"mode": "dark", "custom_enabled": False}, headers=headers)
    r = client.get("/organization/theme", headers=headers)
    body = r.json()
    assert body["custom_enabled"] is False
    assert body["theme"]["mode"] == "dark"  # stored value still returned; frontend decides not to apply it


def test_custom_enabled_true_returns_the_customization():
    headers, _ = _register_org("theme_enabled")
    client.patch("/organization/theme", json={"mode": "dark", "custom_enabled": True}, headers=headers)
    r = client.get("/organization/theme", headers=headers)
    body = r.json()
    assert body["custom_enabled"] is True
    assert body["theme"]["mode"] == "dark"


# ------------------------------- 9-13. permissions / isolation ---------------------


def test_admin_can_access_theme():
    """The self-registering owner has system_role="admin" — the same tier
    every Admin gets, whether they're the original owner or invited later
    (app/routers/users.py creates staff at system_role="staff", never
    "admin", so there is no separate "invite another Admin" flow to exercise
    here beyond the owner's own account)."""
    headers, _ = _register_org("theme_perm_admin")
    r = client.get("/organization/theme", headers=headers)
    assert r.status_code == 200


def test_business_owner_can_access_theme():
    """The self-registering user IS the Business Owner — system_role="admin",
    same tier as any other Admin (see app/services/auth_service.py)."""
    headers, _ = _register_org("theme_perm_bo")
    r = client.get("/organization/theme", headers=headers)
    assert r.status_code == 200
    r2 = client.patch("/organization/theme", json={"mode": "dark"}, headers=headers)
    assert r2.status_code == 200


def test_staff_cannot_access_theme():
    owner_headers, _ = _register_org("theme_perm_staff")
    for role in ("Sales Officer", "Delivery Partner", "Accountant"):
        staff_headers = _make_staff(owner_headers, role)
        r = client.get("/organization/theme", headers=staff_headers)
        assert r.status_code == 403, f"{role} should be denied, got {r.status_code}"
        r2 = client.patch("/organization/theme", json={"mode": "dark"}, headers=staff_headers)
        assert r2.status_code == 403, f"{role} PATCH should be denied, got {r2.status_code}"


def test_organization_a_cannot_access_organization_b_theme():
    headers_a, _ = _register_org("theme_iso_a")
    headers_b, _ = _register_org("theme_iso_b")
    client.patch("/organization/theme", json={"mode": "dark", "primary_color": "#111111"}, headers=headers_a)

    r_b = client.get("/organization/theme", headers=headers_b)
    assert r_b.json()["theme"]["mode"] == "light"  # B sees its own default, not A's dark theme
    assert r_b.json()["theme"]["colors"]["primary"] is None


def test_organization_id_cannot_be_supplied_to_bypass_isolation():
    headers_a, org_id_a = _register_org("theme_bypass_a")
    headers_b, org_id_b = _register_org("theme_bypass_b")
    client.patch("/organization/theme", json={"mode": "dark"}, headers=headers_a)

    # Even if a client sends organization_id in the body, there is no field
    # for it in OrganizationThemeUpdate — Pydantic silently ignores unknown
    # keys by default, and the server always derives the org from the token.
    r = client.patch(
        "/organization/theme",
        json={"organization_id": org_id_a, "mode": "dark"},
        headers=headers_b,
    )
    assert r.status_code == 200
    db = SessionLocal()
    try:
        theme_b = db.query(OrganizationTheme).filter(OrganizationTheme.organization_id == org_id_b).first()
        assert theme_b is not None  # the write landed on B's own org, not A's
        theme_a = db.query(OrganizationTheme).filter(OrganizationTheme.organization_id == org_id_a).first()
        assert theme_a.mode == "dark" and theme_a.organization_id == org_id_a  # A's row untouched by B's request
    finally:
        db.close()


# ------------------------------- 14-20. field validation ---------------------------


def test_valid_hex_colors_accepted():
    headers, _ = _register_org("theme_hex_ok")
    for color in ("#fff", "#ffffff", "#22c55e"):
        r = client.patch("/organization/theme", json={"primary_color": color}, headers=headers)
        assert r.status_code == 200, (color, r.text)
        assert r.json()["theme"]["colors"]["primary"] == color


def test_invalid_color_values_rejected():
    headers, _ = _register_org("theme_hex_bad")
    for color in ("red", "rgb(1,2,3)", "rgba(1,2,3,0.5)", "javascript:alert(1)", "#gggggg"):
        r = client.patch("/organization/theme", json={"primary_color": color}, headers=headers)
        assert r.status_code == 422, (color, r.text)


def test_valid_mode_accepted():
    headers, _ = _register_org("theme_mode_ok")
    for mode in ("light", "dark"):
        r = client.patch("/organization/theme", json={"mode": mode}, headers=headers)
        assert r.status_code == 200


def test_invalid_mode_rejected():
    headers, _ = _register_org("theme_mode_bad")
    r = client.patch("/organization/theme", json={"mode": "blue"}, headers=headers)
    assert r.status_code == 422


def test_valid_theme_name_accepted():
    headers, _ = _register_org("theme_name_ok")
    for name in ("default", "professional", "dark", "custom"):
        r = client.patch("/organization/theme", json={"theme_name": name}, headers=headers)
        assert r.status_code == 200


def test_invalid_theme_name_rejected():
    headers, _ = _register_org("theme_name_bad")
    r = client.patch("/organization/theme", json={"theme_name": "hacked"}, headers=headers)
    assert r.status_code == 422


def test_overlay_opacity_validation():
    headers, _ = _register_org("theme_overlay")
    assert client.patch("/organization/theme", json={"overlay_opacity": 0.0}, headers=headers).status_code == 200
    assert client.patch("/organization/theme", json={"overlay_opacity": 1.0}, headers=headers).status_code == 200
    assert client.patch("/organization/theme", json={"overlay_opacity": 0.65}, headers=headers).status_code == 200
    assert client.patch("/organization/theme", json={"overlay_opacity": 1.5}, headers=headers).status_code == 422
    assert client.patch("/organization/theme", json={"overlay_opacity": -0.1}, headers=headers).status_code == 422


# ------------------------------- 21-28. uploads -------------------------------------


def test_png_upload_accepted():
    headers, _ = _register_org("theme_png")
    r = client.post(
        "/organization/theme/background",
        files={"file": ("bg.png", io.BytesIO(_png_bytes()), "image/png")},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["theme"]["background"]["url"].startswith("/files/")


def test_jpeg_upload_accepted():
    headers, _ = _register_org("theme_jpeg")
    r = client.post(
        "/organization/theme/background",
        files={"file": ("bg.jpg", io.BytesIO(b"fake-jpeg-bytes"), "image/jpeg")},
        headers=headers,
    )
    assert r.status_code == 200, r.text


def test_webp_upload_accepted():
    headers, _ = _register_org("theme_webp")
    r = client.post(
        "/organization/theme/logo",
        files={"file": ("logo.webp", io.BytesIO(b"fake-webp-bytes"), "image/webp")},
        headers=headers,
    )
    assert r.status_code == 200, r.text


def test_unsupported_file_type_rejected():
    headers, _ = _register_org("theme_bad_type")
    for content_type, name in [("image/gif", "a.gif"), ("application/pdf", "a.pdf"), ("image/svg+xml", "a.svg")]:
        r = client.post(
            "/organization/theme/background",
            files={"file": (name, io.BytesIO(b"data"), content_type)},
            headers=headers,
        )
        assert r.status_code == 400, (content_type, r.text)


def test_file_over_5mb_rejected():
    headers, _ = _register_org("theme_too_big")
    oversized = b"\x00" * (5 * 1024 * 1024 + 1)
    r = client.post(
        "/organization/theme/background",
        files={"file": ("big.png", io.BytesIO(oversized), "image/png")},
        headers=headers,
    )
    assert r.status_code == 413, r.text


def test_background_upload_stores_files_id_url():
    headers, _ = _register_org("theme_bg_url")
    r = client.post(
        "/organization/theme/background",
        files={"file": ("bg.png", io.BytesIO(_png_bytes()), "image/png")},
        headers=headers,
    )
    url = r.json()["theme"]["background"]["url"]
    assert url.startswith("/files/")
    for fragment in ("http://", "https://", "onrender.com", "asynk.in", "workers.dev"):
        assert fragment not in url


def test_logo_upload_stores_files_id_url():
    headers, _ = _register_org("theme_logo_url")
    r = client.post(
        "/organization/theme/logo",
        files={"file": ("logo.png", io.BytesIO(_png_bytes()), "image/png")},
        headers=headers,
    )
    url = r.json()["theme"]["logo_url"]
    assert url.startswith("/files/")


def test_theme_logo_does_not_modify_organization_logo_url():
    headers, org_id = _register_org("theme_logo_isolated")
    db = SessionLocal()
    try:
        org = db.get(Organization, org_id)
        org.logo_url = "/files/existing-company-logo"
        db.commit()
    finally:
        db.close()

    client.post(
        "/organization/theme/logo",
        files={"file": ("logo.png", io.BytesIO(_png_bytes()), "image/png")},
        headers=headers,
    )

    db = SessionLocal()
    try:
        org = db.get(Organization, org_id)
        assert org.logo_url == "/files/existing-company-logo"  # completely untouched
        theme = db.query(OrganizationTheme).filter(OrganizationTheme.organization_id == org_id).first()
        assert theme.logo_url != org.logo_url
        assert theme.logo_url.startswith("/files/")
    finally:
        db.close()


# ---------------------- PUBLIC_BASE_URL: absolute responses, relative DB ----------
# organization_themes.background_image_url / logo_url must stay relative in the
# database regardless of PUBLIC_BASE_URL — only the API response is affected.

_PUBLIC_BASE_URL = "https://crm-saas-backend.bsmart.workers.dev"


def test_background_upload_response_absolute_when_public_base_url_configured(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", _PUBLIC_BASE_URL)
    headers, org_id = _register_org("theme_bg_absolute")
    r = client.post(
        "/organization/theme/background",
        files={"file": ("bg.png", io.BytesIO(_png_bytes()), "image/png")},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    url = r.json()["theme"]["background"]["url"]
    assert url.startswith(_PUBLIC_BASE_URL + "/files/")

    db = SessionLocal()
    try:
        theme = db.query(OrganizationTheme).filter(OrganizationTheme.organization_id == org_id).first()
        assert theme.background_image_url.startswith("/files/")
        assert _PUBLIC_BASE_URL not in theme.background_image_url
    finally:
        db.close()


def test_theme_logo_upload_response_absolute_when_public_base_url_configured(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", _PUBLIC_BASE_URL)
    headers, org_id = _register_org("theme_logo_absolute")
    r = client.post(
        "/organization/theme/logo",
        files={"file": ("logo.png", io.BytesIO(_png_bytes()), "image/png")},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    url = r.json()["theme"]["logo_url"]
    assert url.startswith(_PUBLIC_BASE_URL + "/files/")

    db = SessionLocal()
    try:
        theme = db.query(OrganizationTheme).filter(OrganizationTheme.organization_id == org_id).first()
        assert theme.logo_url.startswith("/files/")
        assert _PUBLIC_BASE_URL not in theme.logo_url
    finally:
        db.close()


def test_get_theme_response_absolute_when_public_base_url_configured(monkeypatch):
    headers, org_id = _register_org("theme_get_absolute")
    client.post(
        "/organization/theme/logo",
        files={"file": ("logo.png", io.BytesIO(_png_bytes()), "image/png")},
        headers=headers,
    )

    monkeypatch.setattr(settings, "public_base_url", _PUBLIC_BASE_URL)
    r = client.get("/organization/theme", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["theme"]["logo_url"].startswith(_PUBLIC_BASE_URL + "/files/")

    db = SessionLocal()
    try:
        theme = db.query(OrganizationTheme).filter(OrganizationTheme.organization_id == org_id).first()
        assert theme.logo_url.startswith("/files/")  # GET never mutated the stored value
    finally:
        db.close()


def test_patch_theme_response_absolute_when_public_base_url_configured(monkeypatch):
    headers, org_id = _register_org("theme_patch_absolute")
    client.post(
        "/organization/theme/background",
        files={"file": ("bg.png", io.BytesIO(_png_bytes()), "image/png")},
        headers=headers,
    )

    monkeypatch.setattr(settings, "public_base_url", _PUBLIC_BASE_URL)
    r = client.patch("/organization/theme", json={"mode": "dark"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["theme"]["background"]["url"].startswith(_PUBLIC_BASE_URL + "/files/")

    db = SessionLocal()
    try:
        theme = db.query(OrganizationTheme).filter(OrganizationTheme.organization_id == org_id).first()
        assert theme.background_image_url.startswith("/files/")
    finally:
        db.close()


def test_theme_response_relative_when_public_base_url_unset():
    assert settings.public_base_url == ""  # sanity
    headers, _ = _register_org("theme_relative_default")
    client.post(
        "/organization/theme/logo",
        files={"file": ("logo.png", io.BytesIO(_png_bytes()), "image/png")},
        headers=headers,
    )
    r = client.get("/organization/theme", headers=headers)
    assert r.json()["theme"]["logo_url"].startswith("/files/")


# ------------------------------- 29-31. activity logging ---------------------------


def test_activity_log_created_for_theme_update():
    headers, org_id = _register_org("theme_log_update")
    client.patch("/organization/theme", json={"mode": "dark"}, headers=headers)
    db = SessionLocal()
    try:
        entry = (
            db.query(ActivityLog)
            .filter(ActivityLog.organization_id == org_id, ActivityLog.title == "Theme updated")
            .first()
        )
        assert entry is not None
    finally:
        db.close()


def test_activity_log_created_for_background_upload():
    headers, org_id = _register_org("theme_log_bg")
    client.post(
        "/organization/theme/background",
        files={"file": ("bg.png", io.BytesIO(_png_bytes()), "image/png")},
        headers=headers,
    )
    db = SessionLocal()
    try:
        entry = (
            db.query(ActivityLog)
            .filter(ActivityLog.organization_id == org_id, ActivityLog.title == "Background uploaded")
            .first()
        )
        assert entry is not None
    finally:
        db.close()


def test_activity_log_created_for_logo_change():
    headers, org_id = _register_org("theme_log_logo")
    client.post(
        "/organization/theme/logo",
        files={"file": ("logo.png", io.BytesIO(_png_bytes()), "image/png")},
        headers=headers,
    )
    db = SessionLocal()
    try:
        entry = (
            db.query(ActivityLog)
            .filter(ActivityLog.organization_id == org_id, ActivityLog.title == "Logo changed")
            .first()
        )
        assert entry is not None
    finally:
        db.close()


# ------------------------------- 32-34. persistence / regression -------------------


def test_custom_config_persists_correctly():
    headers, _ = _register_org("theme_custom_config")
    payload = {"custom_config": {"nav_style": "sidebar", "extra": {"nested": 1}}}
    client.patch("/organization/theme", json=payload, headers=headers)
    r = client.get("/organization/theme", headers=headers)
    assert r.json()["theme"]["custom_config"] == payload["custom_config"]


def test_existing_organization_records_remain_unaffected():
    """Registering, using unrelated org fields, and touching the theme must
    not disturb any of Organization's own pre-existing branding columns."""
    headers, org_id = _register_org("theme_no_side_effects")
    db = SessionLocal()
    try:
        org = db.get(Organization, org_id)
        org.logo_url = "/files/original-logo"
        org.signature_url = "/files/original-signature"
        db.commit()
    finally:
        db.close()

    client.patch("/organization/theme", json={"mode": "dark", "custom_enabled": True}, headers=headers)
    client.post(
        "/organization/theme/logo",
        files={"file": ("logo.png", io.BytesIO(_png_bytes()), "image/png")},
        headers=headers,
    )

    db = SessionLocal()
    try:
        org = db.get(Organization, org_id)
        assert org.logo_url == "/files/original-logo"
        assert org.signature_url == "/files/original-signature"
    finally:
        db.close()


def test_theme_deleted_when_organization_deleted():
    """FK ondelete=CASCADE: deleting the organization row must not leave an
    orphaned organization_themes row behind."""
    headers, org_id = _register_org("theme_cascade")
    client.patch("/organization/theme", json={"mode": "dark"}, headers=headers)

    db = SessionLocal()
    try:
        assert db.query(OrganizationTheme).filter(OrganizationTheme.organization_id == org_id).first() is not None
        org = db.get(Organization, org_id)
        db.delete(org)
        db.commit()

        assert db.query(OrganizationTheme).filter(OrganizationTheme.organization_id == org_id).first() is None
    finally:
        db.close()
