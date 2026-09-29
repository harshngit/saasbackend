"""Tests for the backend file-URL migration tool (app/scripts/migrate_render_urls.py).

Covers detection and relative-path rewriting of absolute backend file URLs
from every supported host (Render, api.asynk.in, the Cloudflare Worker host),
stored_files validation safety, JSON recursion, idempotency, and the
dry-run/--apply CLI contract. Runs only against the local SQLite test
database — never production.
"""

import uuid
import pytest
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models import (
    Customer,
    CustomerDocument,
    Delivery,
    Expense,
    Organization,
    Product,
    StoredFile,
    User,
)
from app.scripts.migrate_render_urls import (
    ABSOLUTE_FILE_URL_PATTERN,
    RELATIVE_FILE_URL_PATTERN,
    audit_and_migrate,
    extract_file_references,
    main,
    transform_file_urls,
    validate_stored_files,
)


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _create_stored_file(
    db: Session,
    *,
    file_id: str | None = None,
    org_id: str | None = None,
    with_storage_key: bool = False,
    with_data: bool = True,
) -> str:
    fid = file_id or str(uuid.uuid4())
    sf = StoredFile(
        id=fid,
        organization_id=org_id,
        filename="test.png",
        content_type="image/png",
        size=100,
        storage_key=f"org/{org_id}/{fid}" if with_storage_key else None,
        data=b"fake-bytes" if with_data else None,
    )
    db.add(sf)
    db.commit()
    return fid


# --------------------------- pattern: supported hosts ------------------------------


@pytest.mark.parametrize(
    "url,expected_id",
    [
        ("https://saasbackend-1-f6v3.onrender.com/files/d4825176-8f39-423e-86f9-758986f896dc", "d4825176-8f39-423e-86f9-758986f896dc"),
        ("http://saasbackend.onrender.com/files/d4825176-8f39-423e-86f9-758986f896dc", "d4825176-8f39-423e-86f9-758986f896dc"),
        ("https://crm-api.onrender.com/files/my-file-id-123", "my-file-id-123"),
        ("https://api.asynk.in/files/d4825176-8f39-423e-86f9-758986f896dc", "d4825176-8f39-423e-86f9-758986f896dc"),
        ("http://api.asynk.in/files/d4825176-8f39-423e-86f9-758986f896dc", "d4825176-8f39-423e-86f9-758986f896dc"),
        ("https://crm-saas-backend.bsmart.workers.dev/files/d4825176-8f39-423e-86f9-758986f896dc", "d4825176-8f39-423e-86f9-758986f896dc"),
        ("http://crm-saas-backend.bsmart.workers.dev/files/d4825176-8f39-423e-86f9-758986f896dc", "d4825176-8f39-423e-86f9-758986f896dc"),
    ],
)
def test_absolute_pattern_matches_every_supported_host(url, expected_id):
    match = ABSOLUTE_FILE_URL_PATTERN.search(url)
    assert match is not None, f"expected a match for {url}"
    assert match.group("fid") == expected_id


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/files/d4825176-8f39-423e-86f9-758986f896dc",
        "https://saasbackend.onrender.com/other/path/image.png",
        "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg==",
        "https://facebook.com/mycompany",
        "https://evil.example.com/files/abc",  # unrelated host — must never match
    ],
)
def test_absolute_pattern_ignores_unrelated_urls(url):
    assert ABSOLUTE_FILE_URL_PATTERN.search(url) is None


def test_relative_pattern_matches_bare_files_path_only():
    match = RELATIVE_FILE_URL_PATTERN.search("/files/d4825176-8f39-423e-86f9-758986f896dc")
    assert match is not None
    assert match.group("fid") == "d4825176-8f39-423e-86f9-758986f896dc"


def test_relative_pattern_does_not_match_inside_absolute_urls():
    """The /files/<id> suffix of an absolute URL must not also be counted as
    a separate 'already relative' reference — only a genuinely bare path."""
    for url in [
        "https://api.asynk.in/files/abc123",
        "https://foo.onrender.com/files/abc123",
        "https://crm-saas-backend.bsmart.workers.dev/files/abc123",
        "https://evil.example.com/files/abc123",
    ]:
        assert RELATIVE_FILE_URL_PATTERN.search(url) is None, f"false relative match inside {url}"


# ------------------------------- extraction / transform -----------------------------


def test_extract_file_references_handles_scalar_and_nested_structures():
    id1, id2, id3 = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    data = {
        "single": f"https://saasbackend.onrender.com/files/{id1}",
        "nested_dict": {
            "url": f"https://api.asynk.in/files/{id2}",
            "irrelevant": "https://example.com/logo.png",
        },
        "list_items": [
            f"http://crm-saas-backend.bsmart.workers.dev/files/{id3}",
            "/files/already-relative",
            {"inner_url": f"https://render.onrender.com/files/{id1}"},
        ],
    }
    refs = extract_file_references(data)
    fids = sorted(r["fid"] for r in refs)
    assert fids == sorted([id1, id2, id3, id1, "already-relative"])
    hosts = {r["fid"]: r["host"] for r in refs if r["fid"] != id1 or r["host"] != "render"}
    assert any(r["host"] == "relative" and r["fid"] == "already-relative" for r in refs)


def test_transform_rewrites_every_supported_host_to_relative():
    fid = str(uuid.uuid4())
    for url in [
        f"https://foo.onrender.com/files/{fid}",
        f"http://foo.onrender.com/files/{fid}",
        f"https://api.asynk.in/files/{fid}",
        f"http://api.asynk.in/files/{fid}",
        f"https://crm-saas-backend.bsmart.workers.dev/files/{fid}",
        f"http://crm-saas-backend.bsmart.workers.dev/files/{fid}",
    ]:
        new_val, tr, sk = transform_file_urls(url, {fid})
        assert new_val == f"/files/{fid}", url
        assert tr == 1 and sk == 0


def test_transform_never_produces_a_host_based_url():
    fid = str(uuid.uuid4())
    new_val, _, _ = transform_file_urls(f"https://api.asynk.in/files/{fid}", {fid})
    assert "asynk.in" not in new_val
    assert "onrender.com" not in new_val
    assert "workers.dev" not in new_val
    assert new_val == f"/files/{fid}"


def test_transform_leaves_missing_id_untouched():
    render_url = f"https://saasbackend-1-f6v3.onrender.com/files/{uuid.uuid4()}"
    new_val, tr, sk = transform_file_urls(render_url, set())
    assert new_val == render_url
    assert tr == 0 and sk == 1


def test_transform_leaves_already_relative_untouched():
    new_val, tr, sk = transform_file_urls("/files/already-there", {"already-there"})
    assert new_val == "/files/already-there"
    assert tr == 0 and sk == 0


def test_transform_leaves_unrelated_urls_untouched():
    new_val, tr, sk = transform_file_urls("https://facebook.com/mycompany", set())
    assert new_val == "https://facebook.com/mycompany"
    assert tr == 0 and sk == 0


# ------------------------------- stored_files validation ----------------------------


def test_validate_stored_files_logic(db: Session):
    org = Organization(name="SF Validate Org")
    db.add(org)
    db.commit()

    id_db = _create_stored_file(db, org_id=org.id, with_data=True, with_storage_key=False)
    id_r2 = _create_stored_file(db, org_id=org.id, with_data=False, with_storage_key=True)
    id_corrupt = _create_stored_file(db, org_id=org.id, with_data=False, with_storage_key=False)
    id_missing = str(uuid.uuid4())

    valid, missing = validate_stored_files(db, {id_db, id_r2, id_corrupt, id_missing})

    assert id_db in valid
    assert id_r2 in valid
    assert id_corrupt in missing  # exists but neither storage_key nor data set -> invalid
    assert id_missing in missing


# ----------------------------------- dry-run / apply --------------------------------


def test_dry_run_makes_zero_database_modifications(db: Session):
    org = Organization(name="Dry Run Org")
    db.add(org)
    db.commit()

    fid = _create_stored_file(db, org_id=org.id)
    render_url = f"https://saasbackend-1-f6v3.onrender.com/files/{fid}"
    org.logo_url = render_url
    db.commit()

    result = audit_and_migrate(db, apply=False)

    assert result.total_references >= 1
    assert fid in result.unique_file_ids
    assert fid in result.valid_file_ids
    assert result.eligible_rewrites >= 1

    db.expire_all()
    fresh_org = db.get(Organization, org.id)
    assert fresh_org.logo_url == render_url  # unchanged


@pytest.mark.parametrize(
    "url_template",
    [
        "https://saasbackend-1-f6v3.onrender.com/files/{fid}",
        "https://api.asynk.in/files/{fid}",
        "https://crm-saas-backend.bsmart.workers.dev/files/{fid}",
    ],
)
def test_apply_mode_rewrites_every_supported_host_to_relative(db: Session, url_template):
    org = Organization(name=f"Apply Org {uuid.uuid4().hex[:6]}")
    db.add(org)
    db.commit()

    valid_id = _create_stored_file(db, org_id=org.id)
    missing_id = str(uuid.uuid4())

    org.logo_url = url_template.format(fid=valid_id)
    org.banner_url = url_template.format(fid=missing_id)
    db.commit()

    result = audit_and_migrate(db, apply=True)

    assert valid_id in result.valid_file_ids
    assert missing_id in result.missing_file_ids

    db.expire_all()
    fresh_org = db.get(Organization, org.id)
    assert fresh_org.logo_url == f"/files/{valid_id}"
    assert fresh_org.banner_url == url_template.format(fid=missing_id)  # untouched


def test_json_structures_rewritten_in_apply_mode(db: Session):
    org = Organization(name="JSON Test Org")
    db.add(org)
    db.commit()

    fid1 = _create_stored_file(db, org_id=org.id)
    fid2 = _create_stored_file(db, org_id=org.id)

    user = User(
        organization_id=org.id,
        name="Test User",
        email=f"user_{uuid.uuid4().hex[:6]}@example.com",
        password_hash="dummy_hash",
        uploaded_documents=[
            {"name": "Doc 1", "url": f"https://saasbackend.onrender.com/files/{fid1}"},
            {"name": "Doc 2", "url": "https://external.com/doc.pdf"},
        ],
    )
    db.add(user)

    product = Product(
        organization_id=org.id,
        name="Test Product",
        price=100.0,
        images=[
            f"https://api.asynk.in/files/{fid2}",
            "/files/local-image",
        ],
    )
    db.add(product)
    db.commit()

    audit_and_migrate(db, apply=True)

    db.expire_all()
    fresh_user = db.get(User, user.id)
    assert fresh_user.uploaded_documents[0]["url"] == f"/files/{fid1}"
    assert fresh_user.uploaded_documents[1]["url"] == "https://external.com/doc.pdf"

    fresh_prod = db.get(Product, product.id)
    assert fresh_prod.images[0] == f"/files/{fid2}"
    assert fresh_prod.images[1] == "/files/local-image"  # already relative, untouched


def test_already_relative_reference_is_reported_but_not_rewritten(db: Session):
    org = Organization(name="Already Relative Org")
    db.add(org)
    db.commit()

    fid = _create_stored_file(db, org_id=org.id)
    org.logo_url = f"/files/{fid}"
    db.commit()

    result = audit_and_migrate(db, apply=True)

    assert result.relative_references >= 1
    assert result.eligible_rewrites == 0  # nothing to rewrite — already relative

    db.expire_all()
    fresh_org = db.get(Organization, org.id)
    assert fresh_org.logo_url == f"/files/{fid}"  # unchanged


def test_idempotency_of_migration(db: Session):
    org = Organization(name="Idempotency Org")
    db.add(org)
    db.commit()

    fid = _create_stored_file(db, org_id=org.id)
    org.logo_url = f"https://saasbackend-1-f6v3.onrender.com/files/{fid}"
    db.commit()

    res1 = audit_and_migrate(db, apply=True)
    assert fid in res1.unique_file_ids
    assert res1.rows_modified >= 1

    res2 = audit_and_migrate(db, apply=True)
    assert res2.eligible_rewrites == 0
    assert res2.rows_modified == 0
    # The now-relative reference is still correctly detected and reported.
    assert fid in res2.unique_file_ids
    assert res2.relative_references >= 1


def test_cli_main_dry_run_and_apply(db: Session):
    org = Organization(name="CLI Test Org")
    db.add(org)
    db.commit()

    fid = _create_stored_file(db, org_id=org.id)
    org.logo_url = f"https://saasbackend-1-f6v3.onrender.com/files/{fid}"
    db.commit()

    rc_dry = main(["--dry-run"])
    assert rc_dry == 0

    db.expire_all()
    assert db.get(Organization, org.id).logo_url != f"/files/{fid}"  # dry-run made no change

    rc_apply = main(["--apply"])
    assert rc_apply == 0

    db.expire_all()
    assert db.get(Organization, org.id).logo_url == f"/files/{fid}"


def test_cli_help_documents_the_relative_path_behavior(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    assert "/files/<id>" in out
    assert "Dry-run is the default" in out
    assert "--apply is required" in out


def test_no_target_base_url_flag_exists():
    """The old absolute-host --target-base-url option must not exist — this
    migration has no target host concept any more."""
    with pytest.raises(SystemExit):
        main(["--target-base-url", "https://example.com"])


# --------------------------------- reporting breakdown -------------------------------


def test_report_distinguishes_host_categories(db: Session):
    org = Organization(name="Report Categories Org")
    db.add(org)
    db.commit()

    render_id = _create_stored_file(db, org_id=org.id)
    asynk_id = _create_stored_file(db, org_id=org.id)
    workers_id = _create_stored_file(db, org_id=org.id)
    relative_id = _create_stored_file(db, org_id=org.id)
    missing_id = str(uuid.uuid4())

    org.logo_url = f"https://saasbackend.onrender.com/files/{render_id}"
    org.signature_url = f"https://api.asynk.in/files/{asynk_id}"
    org.stamp_url = f"https://crm-saas-backend.bsmart.workers.dev/files/{workers_id}"
    org.banner_url = f"/files/{relative_id}"
    org.payment_qr_url = f"https://saasbackend.onrender.com/files/{missing_id}"
    db.commit()

    result = audit_and_migrate(db, apply=False)

    assert result.render_references >= 1
    assert result.asynk_references >= 1
    assert result.workers_references >= 1
    assert result.relative_references >= 1
    assert missing_id in result.missing_file_ids
    assert render_id in result.valid_file_ids
    assert asynk_id in result.valid_file_ids
    assert workers_id in result.valid_file_ids
    assert relative_id in result.valid_file_ids

    key = ("Organization", "logo_url", "render")
    assert key in result.column_breakdown
    assert render_id in result.column_breakdown[key]["valid_ids"]
