"""Organization Theme & Appearance Customization (app/routers/settings.py's
/organization/theme endpoints, app/services/theme_service.py,
app/models/organization_theme.py).

Covers:
- Simplified canonical theme model (custom_enabled, mode, primary_color, background)
- Default behavior when no row exists yet (custom_enabled=false, mode=light, overlay_opacity=0.45)
- Permission matrix: any active org user & staff can GET (200); only Admin/Business Owner can PATCH, POST/DELETE background, and POST reset.
- Super Admin GET returns defaults without error.
- Strict validation (6-digit hex only, lowercase normalization, #fff rejected, overlay_opacity 0.0-0.9 with 1.0 rejected).
- Pydantic extra="forbid" (unknown/removed fields return 422).
- Background file lifecycle (PNG/JPEG/WebP uploads, 5MB limit, replacing deletes old StoredFile & R2 object, DELETE background, POST reset).
- Endpoint removal (POST /organization/theme/logo is removed/404).
- Tenant isolation across organizations A and B.
- Activity logging on all mutations.
- PUBLIC_BASE_URL response normalization.
- /auth/me theme inclusion.
"""

import io
import os
import sys
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.security import create_access_token
from app.main import app
from app.models import ActivityLog, Organization, OrganizationTheme, StoredFile, SystemRole, User, UserRole
from app.seed import main as seed_main

seed_main()
client = TestClient(app)


def _register_org(name_prefix: str) -> tuple[dict, str]:
    """Registers a brand-new organization; registering user has system_role="admin"."""
    email = f"{name_prefix}_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post(
        "/auth/register",
        json={
            "organization_name": f"{name_prefix} {uuid.uuid4().hex[:6]}",
            "admin_name": "Owner",
            "email": email,
            "password": "Password123!",
            "role": "admin",
        },
    )
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
    """Creates a staff user with a seeded role and logs in."""
    email = f"staff_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post(
        "/users",
        json={
            "name": "Staff",
            "email": email,
            "password": "Password123!",
            "role": role,
        },
        headers=owner_headers,
    )
    assert r.status_code == 201, r.text
    r2 = client.post("/auth/login", json={"email": email, "password": "Password123!"})
    assert r2.status_code == 200, r2.text
    return {"Authorization": f"Bearer {r2.json()['tokens']['access_token']}"}


def _make_super_admin() -> dict:
    """Creates a Super Admin user directly in DB and returns auth headers."""
    db = SessionLocal()
    try:
        email = f"superadmin_{uuid.uuid4().hex[:8]}@example.com"
        user = User(
            name="Super Admin",
            email=email,
            password_hash="hash",
            role=UserRole.SUPER_ADMIN,
            system_role=SystemRole.SUPER_ADMIN.value,
            organization_id=None,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        token = create_access_token(user.id, role="super_admin", organization_id=None)
        return {"Authorization": f"Bearer {token}"}
    finally:
        db.close()


def _png_bytes() -> bytes:
    return b"fake-png-bytes"


# ------------------------------- 1. Defaults & Canonical Response Shape -----------------------


def test_default_theme_behavior():
    headers, _ = _register_org("theme_default")
    r = client.get("/organization/theme", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()

    # Canonical top-level fields
    assert body["custom_enabled"] is False
    assert body["mode"] == "light"
    assert body["primary_color"] is None
    assert body["background"] == {"url": None, "overlay_opacity": 0.45}
    assert body["updated_at"] is None

    # Assert deprecated fields are NOT present
    assert "theme" not in body
    assert "theme_name" not in body
    assert "fonts" not in body
    assert "colors" not in body
    assert "card_style" not in body
    assert "border_radius" not in body
    assert "background_image_url" not in body
    assert "overlay" not in body.get("background", {})


def test_get_theme_when_no_row_exists_does_not_create_one():
    headers, org_id = _register_org("theme_no_row")
    client.get("/organization/theme", headers=headers)
    db = SessionLocal()
    try:
        assert (
            db.query(OrganizationTheme)
            .filter(OrganizationTheme.organization_id == org_id)
            .first()
            is None
        )
    finally:
        db.close()


# ------------------------------- 2. Permissions ----------------------------------------------


def test_admin_and_business_owner_can_get_and_patch_theme():
    headers, _ = _register_org("theme_admin_bo")
    # GET
    r = client.get("/organization/theme", headers=headers)
    assert r.status_code == 200
    # PATCH
    r2 = client.patch(
        "/organization/theme",
        json={"mode": "dark", "primary_color": "#22c55e", "custom_enabled": True},
        headers=headers,
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body["custom_enabled"] is True
    assert body["mode"] == "dark"
    assert body["primary_color"] == "#22c55e"


def test_staff_role_can_get_theme_but_cannot_mutate():
    owner_headers, _ = _register_org("theme_staff_perm")
    for role in ("Sales Officer", "Delivery Partner", "Accountant"):
        staff_headers = _make_staff(owner_headers, role)

        # Staff can GET theme (200 OK)
        r_get = client.get("/organization/theme", headers=staff_headers)
        assert r_get.status_code == 200, f"{role} should be able to GET theme, got {r_get.status_code}"

        # Staff gets 403 on PATCH
        r_patch = client.patch("/organization/theme", json={"mode": "dark"}, headers=staff_headers)
        assert r_patch.status_code == 403, f"{role} PATCH should be 403"

        # Staff gets 403 on background upload
        r_bg = client.post(
            "/organization/theme/background",
            files={"file": ("bg.png", io.BytesIO(_png_bytes()), "image/png")},
            headers=staff_headers,
        )
        assert r_bg.status_code == 403, f"{role} background upload should be 403"

        # Staff gets 403 on background DELETE
        r_del = client.delete("/organization/theme/background", headers=staff_headers)
        assert r_del.status_code == 403, f"{role} background delete should be 403"

        # Staff gets 403 on reset
        r_reset = client.post("/organization/theme/reset", headers=staff_headers)
        assert r_reset.status_code == 403, f"{role} reset should be 403"


def test_super_admin_get_returns_defaults_without_error():
    sa_headers = _make_super_admin()
    r = client.get("/organization/theme", headers=sa_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["custom_enabled"] is False
    assert body["mode"] == "light"
    assert body["primary_color"] is None
    assert body["background"]["url"] is None
    assert body["background"]["overlay_opacity"] == 0.45
    assert body["updated_at"] is None


# ------------------------------- 3. PATCH Semantics & Field Validation -----------------------


def test_patch_creates_theme_row_when_missing():
    headers, org_id = _register_org("theme_patch_create")
    r = client.patch("/organization/theme", json={"custom_enabled": True}, headers=headers)
    assert r.status_code == 200, r.text
    db = SessionLocal()
    try:
        row = (
            db.query(OrganizationTheme)
            .filter(OrganizationTheme.organization_id == org_id)
            .first()
        )
        assert row is not None
        assert row.custom_enabled is True
        assert row.mode == "light"
        assert row.overlay_opacity == 0.45
    finally:
        db.close()


def test_patch_partial_updates():
    headers, _ = _register_org("theme_partial")
    # Set mode and primary_color
    client.patch(
        "/organization/theme",
        json={"mode": "dark", "primary_color": "#22C55E"},
        headers=headers,
    )
    # Update only overlay_opacity
    r = client.patch(
        "/organization/theme",
        json={"overlay_opacity": 0.65},
        headers=headers,
    )
    body = r.json()
    assert body["mode"] == "dark"
    assert body["primary_color"] == "#22c55e"  # Lowercase normalized
    assert body["background"]["overlay_opacity"] == 0.65


def test_mode_validation():
    headers, _ = _register_org("theme_mode_val")
    for mode in ("light", "dark"):
        r = client.patch("/organization/theme", json={"mode": mode}, headers=headers)
        assert r.status_code == 200
        assert r.json()["mode"] == mode

    for bad in ("blue", "dim", "custom", "DARK", ""):
        r = client.patch("/organization/theme", json={"mode": bad}, headers=headers)
        assert r.status_code == 422


def test_primary_color_validation_and_normalization():
    headers, _ = _register_org("theme_color_val")

    # 6-digit hex uppercase normalized to lowercase
    r = client.patch("/organization/theme", json={"primary_color": "#22C55E"}, headers=headers)
    assert r.status_code == 200
    assert r.json()["primary_color"] == "#22c55e"

    # null accepted
    r2 = client.patch("/organization/theme", json={"primary_color": None}, headers=headers)
    assert r2.status_code == 200
    assert r2.json()["primary_color"] is None

    # Shorthand #fff MUST be rejected (422)
    r3 = client.patch("/organization/theme", json={"primary_color": "#fff"}, headers=headers)
    assert r3.status_code == 422

    # Invalid colors rejected
    for bad in ("red", "rgb(0,0,0)", "rgba(0,0,0,0)", "#12345", "#1234567", "javascript:alert(1)"):
        r_bad = client.patch("/organization/theme", json={"primary_color": bad}, headers=headers)
        assert r_bad.status_code == 422


def test_overlay_opacity_validation():
    headers, _ = _register_org("theme_overlay_val")

    # 0.0 allowed
    assert client.patch("/organization/theme", json={"overlay_opacity": 0.0}, headers=headers).status_code == 200
    # 0.9 allowed
    assert client.patch("/organization/theme", json={"overlay_opacity": 0.9}, headers=headers).status_code == 200
    # 0.55 allowed
    assert client.patch("/organization/theme", json={"overlay_opacity": 0.55}, headers=headers).status_code == 200

    # 1.0 MUST be rejected (422)
    assert client.patch("/organization/theme", json={"overlay_opacity": 1.0}, headers=headers).status_code == 422
    # >0.9 rejected
    assert client.patch("/organization/theme", json={"overlay_opacity": 0.91}, headers=headers).status_code == 422
    # <0.0 rejected
    assert client.patch("/organization/theme", json={"overlay_opacity": -0.1}, headers=headers).status_code == 422


# ------------------------------- 4. Unknown & Removed Fields (HTTP 422) -----------------------


def test_removed_and_unknown_fields_return_422():
    headers, _ = _register_org("theme_unknown_fields")
    for payload in (
        {"heading_font": "DM Sans"},
        {"body_font": "Open Sans"},
        {"theme_name": "custom"},
        {"theme_name": "professional"},
        {"card_style": "glass"},
        {"border_radius": "12px"},
        {"secondary_color": "#16a34a"},
        {"logo_url": "/files/some-id"},
        {"custom_config": {}},
        {"organization_id": "other-id"},
        {"arbitrary_field": "val"},
    ):
        r = client.patch("/organization/theme", json=payload, headers=headers)
        assert r.status_code == 422, f"Expected 422 for payload {payload}, got {r.status_code}: {r.text}"


# ------------------------------- 5. Background File Lifecycle & Cleanups ----------------------


def test_background_upload_png_jpeg_webp():
    headers, _ = _register_org("theme_bg_types")

    # PNG
    r_png = client.post(
        "/organization/theme/background",
        files={"file": ("bg.png", io.BytesIO(_png_bytes()), "image/png")},
        headers=headers,
    )
    assert r_png.status_code == 200
    assert r_png.json()["background"]["url"].startswith("/files/")
    assert r_png.json()["background"]["overlay_opacity"] == 0.45

    # JPEG
    r_jpg = client.post(
        "/organization/theme/background",
        files={"file": ("bg.jpg", io.BytesIO(b"fake-jpeg-bytes"), "image/jpeg")},
        headers=headers,
    )
    assert r_jpg.status_code == 200
    assert r_jpg.json()["background"]["url"].startswith("/files/")

    # WebP
    r_webp = client.post(
        "/organization/theme/background",
        files={"file": ("bg.webp", io.BytesIO(b"fake-webp-bytes"), "image/webp")},
        headers=headers,
    )
    assert r_webp.status_code == 200
    assert r_webp.json()["background"]["url"].startswith("/files/")


def test_background_upload_size_and_mime_validation():
    headers, _ = _register_org("theme_bg_invalid")

    # >5 MB rejected
    oversized = b"\x00" * (5 * 1024 * 1024 + 1)
    r_size = client.post(
        "/organization/theme/background",
        files={"file": ("big.png", io.BytesIO(oversized), "image/png")},
        headers=headers,
    )
    assert r_size.status_code == 413

    # Disallowed MIME types rejected (400)
    for mime, name in (("image/gif", "a.gif"), ("application/pdf", "a.pdf"), ("image/svg+xml", "a.svg")):
        r_mime = client.post(
            "/organization/theme/background",
            files={"file": (name, io.BytesIO(b"data"), mime)},
            headers=headers,
        )
        assert r_mime.status_code == 400


def test_background_replacement_deletes_old_stored_file():
    headers, org_id = _register_org("theme_bg_replace")

    # Upload 1
    r1 = client.post(
        "/organization/theme/background",
        files={"file": ("bg1.png", io.BytesIO(_png_bytes()), "image/png")},
        headers=headers,
    )
    url1 = r1.json()["background"]["url"]
    file_id1 = url1.rsplit("/", 1)[-1]

    db = SessionLocal()
    try:
        assert db.get(StoredFile, file_id1) is not None
    finally:
        db.close()

    # Upload 2 (Replacement)
    r2 = client.post(
        "/organization/theme/background",
        files={"file": ("bg2.png", io.BytesIO(b"new-png-bytes"), "image/png")},
        headers=headers,
    )
    url2 = r2.json()["background"]["url"]
    file_id2 = url2.rsplit("/", 1)[-1]
    assert file_id1 != file_id2

    db = SessionLocal()
    try:
        # Old StoredFile row must be DELETED
        assert db.get(StoredFile, file_id1) is None
        # New StoredFile row must EXIST
        assert db.get(StoredFile, file_id2) is not None
        # DB theme background points to new file
        theme = db.query(OrganizationTheme).filter(OrganizationTheme.organization_id == org_id).first()
        assert theme.background_image_url == f"/files/{file_id2}"
    finally:
        db.close()


def test_delete_background_endpoint():
    headers, org_id = _register_org("theme_bg_delete")

    # Configure theme with color and background
    client.patch(
        "/organization/theme",
        json={"mode": "dark", "primary_color": "#22c55e", "overlay_opacity": 0.7},
        headers=headers,
    )
    r_up = client.post(
        "/organization/theme/background",
        files={"file": ("bg.png", io.BytesIO(_png_bytes()), "image/png")},
        headers=headers,
    )
    file_id = r_up.json()["background"]["url"].rsplit("/", 1)[-1]

    # DELETE /organization/theme/background
    r_del = client.delete("/organization/theme/background", headers=headers)
    assert r_del.status_code == 200
    body = r_del.json()
    assert body["background"]["url"] is None
    assert body["background"]["overlay_opacity"] == 0.7  # Preserved
    assert body["mode"] == "dark"  # Preserved
    assert body["primary_color"] == "#22c55e"  # Preserved

    db = SessionLocal()
    try:
        assert db.get(StoredFile, file_id) is None  # StoredFile deleted
        theme = db.query(OrganizationTheme).filter(OrganizationTheme.organization_id == org_id).first()
        assert theme.background_image_url is None
    finally:
        db.close()

    # Idempotent DELETE when no background exists
    r_del_again = client.delete("/organization/theme/background", headers=headers)
    assert r_del_again.status_code == 200
    assert r_del_again.json()["background"]["url"] is None


def test_reset_organization_theme_endpoint():
    headers, org_id = _register_org("theme_reset")

    # Set custom settings + upload background
    client.patch(
        "/organization/theme",
        json={"custom_enabled": True, "mode": "dark", "primary_color": "#123456", "overlay_opacity": 0.8},
        headers=headers,
    )
    r_up = client.post(
        "/organization/theme/background",
        files={"file": ("bg.png", io.BytesIO(_png_bytes()), "image/png")},
        headers=headers,
    )
    file_id = r_up.json()["background"]["url"].rsplit("/", 1)[-1]

    # POST /organization/theme/reset
    r_reset = client.post("/organization/theme/reset", headers=headers)
    assert r_reset.status_code == 200
    body = r_reset.json()
    assert body["custom_enabled"] is False
    assert body["mode"] == "light"
    assert body["primary_color"] is None
    assert body["background"] == {"url": None, "overlay_opacity": 0.45}
    assert body["updated_at"] is not None

    db = SessionLocal()
    try:
        assert db.get(StoredFile, file_id) is None  # Background file deleted
        theme = db.query(OrganizationTheme).filter(OrganizationTheme.organization_id == org_id).first()
        assert theme.custom_enabled is False
        assert theme.mode == "light"
        assert theme.primary_color is None
        assert theme.background_image_url is None
        assert theme.overlay_opacity == 0.45
    finally:
        db.close()


# ------------------------------- 6. Tenant Isolation -----------------------------------------


def test_tenant_isolation():
    headers_a, org_id_a = _register_org("theme_iso_a")
    headers_b, org_id_b = _register_org("theme_iso_b")

    # A sets theme
    client.patch(
        "/organization/theme",
        json={"custom_enabled": True, "mode": "dark", "primary_color": "#112233"},
        headers=headers_a,
    )
    client.post(
        "/organization/theme/background",
        files={"file": ("bg_a.png", io.BytesIO(_png_bytes()), "image/png")},
        headers=headers_a,
    )

    # B gets theme — sees its own defaults
    r_b = client.get("/organization/theme", headers=headers_b)
    assert r_b.json()["custom_enabled"] is False
    assert r_b.json()["mode"] == "light"
    assert r_b.json()["primary_color"] is None
    assert r_b.json()["background"]["url"] is None

    # B deletes background — does not affect A
    client.delete("/organization/theme/background", headers=headers_b)
    r_a = client.get("/organization/theme", headers=headers_a)
    assert r_a.json()["custom_enabled"] is True
    assert r_a.json()["mode"] == "dark"
    assert r_a.json()["background"]["url"] is not None


# ------------------------------- 7. Removed Endpoint & Logo Independence ----------------------


def test_removed_theme_logo_endpoint_returns_404():
    headers, _ = _register_org("theme_no_logo_ep")
    r = client.post(
        "/organization/theme/logo",
        files={"file": ("logo.png", io.BytesIO(_png_bytes()), "image/png")},
        headers=headers,
    )
    assert r.status_code in (404, 405), f"Expected 404/405 for removed endpoint, got {r.status_code}"


def test_company_logo_behavior_remains_unaffected():
    headers, org_id = _register_org("theme_org_logo_intact")
    db = SessionLocal()
    try:
        org = db.get(Organization, org_id)
        org.logo_url = "/files/company-logo-123"
        db.commit()
    finally:
        db.close()

    # Theme operations
    client.patch(
        "/organization/theme",
        json={"mode": "dark", "custom_enabled": True},
        headers=headers,
    )
    client.post(
        "/organization/theme/background",
        files={"file": ("bg.png", io.BytesIO(_png_bytes()), "image/png")},
        headers=headers,
    )

    db = SessionLocal()
    try:
        org = db.get(Organization, org_id)
        assert org.logo_url == "/files/company-logo-123"
    finally:
        db.close()


# ------------------------------- 8. PUBLIC_BASE_URL Normalization ----------------------------

_PUBLIC_BASE_URL = "https://crm-saas-backend.bsmart.workers.dev"


def test_background_url_absolute_when_public_base_url_set(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", _PUBLIC_BASE_URL)
    headers, org_id = _register_org("theme_pub_base")

    r = client.post(
        "/organization/theme/background",
        files={"file": ("bg.png", io.BytesIO(_png_bytes()), "image/png")},
        headers=headers,
    )
    assert r.status_code == 200
    url = r.json()["background"]["url"]
    assert url.startswith(_PUBLIC_BASE_URL + "/files/")

    # In DB, it must still be relative
    db = SessionLocal()
    try:
        theme = db.query(OrganizationTheme).filter(OrganizationTheme.organization_id == org_id).first()
        assert theme.background_image_url.startswith("/files/")
        assert _PUBLIC_BASE_URL not in theme.background_image_url
    finally:
        db.close()


# ------------------------------- 9. Activity Logging -----------------------------------------


def test_activity_logging_on_theme_mutations():
    headers, org_id = _register_org("theme_act_log")

    # PATCH
    client.patch("/organization/theme", json={"mode": "dark"}, headers=headers)
    # Background upload
    client.post(
        "/organization/theme/background",
        files={"file": ("bg.png", io.BytesIO(_png_bytes()), "image/png")},
        headers=headers,
    )
    # Background delete
    client.delete("/organization/theme/background", headers=headers)
    # Reset
    client.post("/organization/theme/reset", headers=headers)

    db = SessionLocal()
    try:
        titles = {
            log.title
            for log in db.query(ActivityLog).filter(ActivityLog.organization_id == org_id).all()
        }
        assert "Theme updated" in titles
        assert "Background uploaded" in titles
        assert "Background removed" in titles
        assert "Theme reset to defaults" in titles
    finally:
        db.close()


# ------------------------------- 10. /auth/me Theme Inclusion -------------------------------


def test_auth_me_includes_canonical_theme():
    headers, _ = _register_org("theme_auth_me")
    client.patch(
        "/organization/theme",
        json={"custom_enabled": True, "mode": "dark", "primary_color": "#22c55e"},
        headers=headers,
    )

    r = client.get("/auth/me", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert "theme" in body
    assert body["theme"]["custom_enabled"] is True
    assert body["theme"]["mode"] == "dark"
    assert body["theme"]["primary_color"] == "#22c55e"
    assert body["theme"]["background"]["overlay_opacity"] == 0.45
