"""Tests for Render URL migration and audit tool (app/scripts/migrate_render_urls.py)."""

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
    DEFAULT_TARGET_BASE_URL,
    RENDER_URL_PATTERN,
    audit_and_migrate,
    extract_render_file_ids,
    main,
    transform_render_urls,
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


def test_render_url_pattern_matches_http_https_and_subdomains():
    valid_urls = [
        ("https://saasbackend-1-f6v3.onrender.com/files/d4825176-8f39-423e-86f9-758986f896dc", "d4825176-8f39-423e-86f9-758986f896dc"),
        ("http://saasbackend.onrender.com/files/d4825176-8f39-423e-86f9-758986f896dc", "d4825176-8f39-423e-86f9-758986f896dc"),
        ("https://crm-api.onrender.com/files/my-file-id-123", "my-file-id-123"),
    ]
    for url, expected_id in valid_urls:
        match = RENDER_URL_PATTERN.search(url)
        assert match is not None
        assert match.group(1) == expected_id


def test_render_url_pattern_ignores_non_render_and_relative_urls():
    non_matching = [
        "/files/d4825176-8f39-423e-86f9-758986f896dc",
        "https://api.asynk.in/files/d4825176-8f39-423e-86f9-758986f896dc",
        "https://example.com/files/d4825176-8f39-423e-86f9-758986f896dc",
        "https://saasbackend.onrender.com/other/path/image.png",
        "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg==",
        "https://facebook.com/mycompany",
        None,
        "",
    ]
    for url in non_matching:
        if url is None:
            continue
        assert RENDER_URL_PATTERN.search(url) is None


def test_extract_render_file_ids_handles_scalar_and_nested_structures():
    id1 = str(uuid.uuid4())
    id2 = str(uuid.uuid4())
    id3 = str(uuid.uuid4())

    data = {
        "single": f"https://saasbackend.onrender.com/files/{id1}",
        "nested_dict": {
            "url": f"https://saasbackend-1-f6v3.onrender.com/files/{id2}",
            "irrelevant": "https://example.com/logo.png",
        },
        "list_items": [
            f"http://api.onrender.com/files/{id3}",
            "/files/already-relative",
            {"inner_url": f"https://render.onrender.com/files/{id1}"},
        ],
    }

    extracted = extract_render_file_ids(data)
    assert sorted(extracted) == sorted([id1, id2, id3, id1])


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
    assert id_corrupt in missing
    assert id_missing in missing


def test_dry_run_makes_zero_database_modifications(db: Session):
    org = Organization(name="Dry Run Org")
    db.add(org)
    db.commit()

    fid = _create_stored_file(db, org_id=org.id)
    render_url = f"https://saasbackend-1-f6v3.onrender.com/files/{fid}"
    org.logo_url = render_url
    db.commit()

    # Run in dry-run mode (apply=False)
    result = audit_and_migrate(db, apply=False)

    assert result.total_references >= 1
    assert fid in result.unique_file_ids
    assert fid in result.valid_file_ids
    assert result.eligible_rewrites >= 1

    # Verify DB was NOT mutated
    db.expire_all()
    fresh_org = db.get(Organization, org.id)
    assert fresh_org.logo_url == render_url


def test_apply_mode_rewrites_valid_urls_and_leaves_missing_untouched(db: Session):
    org = Organization(name="Apply Org")
    db.add(org)
    db.commit()

    valid_id = _create_stored_file(db, org_id=org.id)
    missing_id = str(uuid.uuid4())

    valid_render_url = f"https://saasbackend-1-f6v3.onrender.com/files/{valid_id}"
    missing_render_url = f"https://saasbackend-1-f6v3.onrender.com/files/{missing_id}"

    org.logo_url = valid_render_url
    org.banner_url = missing_render_url
    db.commit()

    # Run in apply mode
    result = audit_and_migrate(db, apply=True)

    assert valid_id in result.valid_file_ids
    assert missing_id in result.missing_file_ids

    # Verify DB values after commit
    db.expire_all()
    fresh_org = db.get(Organization, org.id)
    assert fresh_org.logo_url == f"https://api.asynk.in/files/{valid_id}"
    assert fresh_org.banner_url == missing_render_url  # Untouched


def test_json_structures_rewritten_in_apply_mode(db: Session):
    org = Organization(name="JSON Test Org")
    db.add(org)
    db.commit()

    fid1 = _create_stored_file(db, org_id=org.id)
    fid2 = _create_stored_file(db, org_id=org.id)

    # User with JSON documents
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

    # Product with JSON string list
    product = Product(
        organization_id=org.id,
        name="Test Product",
        price=100.0,
        images=[
            f"https://saasbackend-1-f6v3.onrender.com/files/{fid2}",
            "/files/local-image",
        ],
    )
    db.add(product)
    db.commit()

    # Run apply
    audit_and_migrate(db, apply=True)

    db.expire_all()
    fresh_user = db.get(User, user.id)
    assert fresh_user.uploaded_documents[0]["url"] == f"https://api.asynk.in/files/{fid1}"
    assert fresh_user.uploaded_documents[1]["url"] == "https://external.com/doc.pdf"

    fresh_prod = db.get(Product, product.id)
    assert fresh_prod.images[0] == f"https://api.asynk.in/files/{fid2}"
    assert fresh_prod.images[1] == "/files/local-image"


def test_idempotency_of_migration(db: Session):
    org = Organization(name="Idempotency Org")
    db.add(org)
    db.commit()

    fid = _create_stored_file(db, org_id=org.id)
    org.logo_url = f"https://saasbackend-1-f6v3.onrender.com/files/{fid}"
    db.commit()

    # First run
    res1 = audit_and_migrate(db, apply=True)
    assert fid in res1.unique_file_ids
    assert res1.rows_modified >= 1

    # Second run
    res2 = audit_and_migrate(db, apply=True)
    assert fid not in res2.unique_file_ids
    assert res2.eligible_rewrites == 0
    assert res2.rows_modified == 0


def test_custom_target_base_url(db: Session):
    org = Organization(name="Custom Target Org")
    db.add(org)
    db.commit()

    fid = _create_stored_file(db, org_id=org.id)
    org.logo_url = f"https://saasbackend-1-f6v3.onrender.com/files/{fid}"
    db.commit()

    custom_url = "https://custom-files.mycompany.com"
    audit_and_migrate(db, apply=True, target_base_url=custom_url)

    db.expire_all()
    fresh_org = db.get(Organization, org.id)
    assert fresh_org.logo_url == f"{custom_url}/files/{fid}"


def test_cli_main_dry_run_and_apply(db: Session):
    org = Organization(name="CLI Test Org")
    db.add(org)
    db.commit()

    fid = _create_stored_file(db, org_id=org.id)
    org.logo_url = f"https://saasbackend-1-f6v3.onrender.com/files/{fid}"
    db.commit()

    # CLI dry-run
    rc_dry = main(["--dry-run"])
    assert rc_dry == 0

    # CLI apply
    rc_apply = main(["--apply"])
    assert rc_apply == 0

    db.expire_all()
    assert db.get(Organization, org.id).logo_url == f"https://api.asynk.in/files/{fid}"
