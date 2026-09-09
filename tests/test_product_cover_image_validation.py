"""Focused Test Suite for Product cover_image Validation & Contract Enforcement.

Verifies:
1. POST /products accepts valid file URLs (full & relative /files/{id}) and null.
2. POST /products rejects data:image/png;base64,..., data:image/jpeg;base64,..., data:image/webp;base64,... with 422.
3. PATCH /products/{id} accepts valid file URLs and preserves existing cover_image when omitted.
4. PATCH /products/{id} rejects data:image/...;base64,... with 422 without modifying the DB value.
5. POST /files/upload and POST /products/{id}/files/cover_image binary upload flows remain fully functional.
"""

import io
import os
import sys
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient
from app.main import app
from app.core.database import SessionLocal
from app.models import Product, StoredFile
from app.seed import main as seed_main

seed_main()
client = TestClient(app)

_passed = 0
_failed = 0


def ok(msg: str):
    global _passed
    _passed += 1
    print(f"  PASS  {msg}")


def fail(msg: str, detail: str = ""):
    global _failed
    _failed += 1
    print(f"  FAIL  {msg}  {detail}")


def assert_eq(actual, expected, msg: str, detail: str = ""):
    if actual == expected:
        ok(msg)
    else:
        fail(msg, f"Expected {expected!r}, got {actual!r}. {detail}")


def register_org(name: str) -> dict:
    email = f"cover_img_{uuid.uuid4().hex[:8]}@{name.lower().replace(' ', '')}.com"
    r = client.post("/auth/register", json={
        "organization_name": name,
        "admin_name": "Admin",
        "email": email,
        "password": "Password123!",
        "role": "admin",
    })
    assert r.status_code == 201, r.text
    token = r.json()["tokens"]["access_token"]
    return {"headers": {"Authorization": f"Bearer {token}"}, "org_id": r.json()["organization"]["id"]}


def test_cover_image_validation_flow():
    print("\n=======================================================")
    print("TEST SUITE: Product cover_image Validation & Contract Enforcement")
    print("=======================================================")

    org = register_org("Cover Image Test Org")
    headers = org["headers"]

    sample_b64_png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    sample_b64_jpeg = "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP------------------------------------------------"
    sample_b64_webp = "data:image/webp;base64,UklGRiIAAABXRUJQVlA4IBYAAAAwAQCdASoBAAEADsD+JaQAA3AAAAAA"

    valid_url_full = "https://example.com/files/test-cover.jpg"
    valid_url_relative = "/files/abc123xyz"

    # --- 1. POST /products: Rejections for data URIs ---
    res_b64_png = client.post("/products", json={
        "name": "Invalid PNG Base64 Product",
        "cover_image": sample_b64_png,
    }, headers=headers)
    assert_eq(res_b64_png.status_code, 422, "POST /products rejects data:image/png;base64,... with 422")

    res_b64_jpeg = client.post("/products", json={
        "name": "Invalid JPEG Base64 Product",
        "cover_image": sample_b64_jpeg,
    }, headers=headers)
    assert_eq(res_b64_jpeg.status_code, 422, "POST /products rejects data:image/jpeg;base64,... with 422")

    res_b64_webp = client.post("/products", json={
        "name": "Invalid WEBP Base64 Product",
        "cover_image": sample_b64_webp,
    }, headers=headers)
    assert_eq(res_b64_webp.status_code, 422, "POST /products rejects data:image/webp;base64,... with 422")

    # --- 2. POST /products: Valid URL & null acceptances ---
    res_valid_full = client.post("/products", json={
        "name": "Valid Full URL Product",
        "cover_image": valid_url_full,
    }, headers=headers)
    assert_eq(res_valid_full.status_code, 201, "POST /products accepts valid full HTTPS URL")
    prod_full = res_valid_full.json()
    assert_eq(prod_full["cover_image"], valid_url_full, "Full HTTPS cover_image stored accurately")
    pid = prod_full["id"]

    res_valid_rel = client.post("/products", json={
        "name": "Valid Relative URL Product",
        "cover_image": valid_url_relative,
    }, headers=headers)
    assert_eq(res_valid_rel.status_code, 201, "POST /products accepts valid relative /files/{id} URL")
    assert_eq(res_valid_rel.json()["cover_image"], valid_url_relative, "Relative /files/{id} cover_image stored accurately")

    res_null = client.post("/products", json={
        "name": "Null Cover Image Product",
        "cover_image": None,
    }, headers=headers)
    assert_eq(res_null.status_code, 201, "POST /products accepts null cover_image")
    assert_eq(res_null.json()["cover_image"], None, "null cover_image stored as null")

    # --- 3. PATCH /products/{id}: Rejections & DB Immutability ---
    patch_b64 = client.patch(f"/products/{pid}", json={
        "cover_image": sample_b64_png,
    }, headers=headers)
    assert_eq(patch_b64.status_code, 422, "PATCH /products/{id} rejects data:image/png;base64,... with 422")

    # Verify DB value was NOT changed by rejected PATCH
    get_check = client.get(f"/products/{pid}", headers=headers)
    assert_eq(get_check.status_code, 200, "GET product detail succeeds")
    assert_eq(get_check.json()["cover_image"], valid_url_full, "DB cover_image remains untouched after rejected PATCH")

    # --- 4. PATCH /products/{id}: Valid updates & field omission ---
    patch_valid = client.patch(f"/products/{pid}", json={
        "cover_image": valid_url_relative,
    }, headers=headers)
    assert_eq(patch_valid.status_code, 200, "PATCH /products/{id} accepts valid /files/{id} update")
    assert_eq(patch_valid.json()["cover_image"], valid_url_relative, "cover_image updated in DB")

    patch_omitted = client.patch(f"/products/{pid}", json={
        "name": "Renamed Product Without Cover Image Field",
    }, headers=headers)
    assert_eq(patch_omitted.status_code, 200, "PATCH /products/{id} succeeds when cover_image is omitted")
    assert_eq(patch_omitted.json()["cover_image"], valid_url_relative, "Existing cover_image preserved when field is omitted in PATCH")

    # --- 5. File Upload Regression Tests ---
    png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4"
    files = {"file": ("test_cover.png", io.BytesIO(png_bytes), "image/png")}
    res_upload = client.post("/files/upload", headers=headers, files=files)
    assert_eq(res_upload.status_code, 201, "POST /files/upload accepts binary multipart file")
    upload_body = res_upload.json()
    assert "/files/" in upload_body["url"], "POST /files/upload returns /files/{id} URL"
    assert "base64," not in upload_body["url"], "POST /files/upload URL contains no base64 string"

    # POST /products/{id}/files/cover_image per-field upload
    cover_file = {"file": ("cover_direct.png", io.BytesIO(png_bytes), "image/png")}
    res_field_upload = client.post(f"/products/{pid}/files/cover_image", headers=headers, files=cover_file)
    assert_eq(res_field_upload.status_code, 200, "POST /products/{id}/files/cover_image succeeds")
    prod_updated = res_field_upload.json()
    assert "/files/" in prod_updated["cover_image"], "Per-field upload updates Product.cover_image to /files/{id} URL"
    assert "base64," not in prod_updated["cover_image"], "Per-field cover_image URL contains no base64 string"

    print("\n=======================================================")
    print(f"RESULTS: {_passed} passed, {_failed} failed")
    print("=======================================================")
    assert _failed == 0, f"{_failed} assertions failed"


if __name__ == "__main__":
    test_cover_image_validation_flow()
