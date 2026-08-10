"""Assemble the Company Settings dashboard summary from the firm's own data.

Everything here is derived on read — there is no summary table to keep in sync,
and the counts are single aggregate queries rather than loaded collections.
"""

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import ActivityLog, Organization, StoredFile, User
from app.schemas.overview import (
    DOCUMENT_SLOTS,
    FIELD_LABELS,
    CompanyOverviewOut,
    OverviewActivity,
    OverviewAddress,
    OverviewAuthorizedPerson,
    OverviewCompany,
    OverviewCounts,
    OverviewDocument,
    OverviewDocuments,
    OverviewPlan,
    OverviewProfileCompletion,
    OverviewStorage,
)
from app.services import activity_service

_BYTES_PER_MB = 1024 * 1024
_BYTES_PER_GB = 1024 * 1024 * 1024


def _plan(org: Organization) -> OverviewPlan:
    plan = org.plan
    return OverviewPlan(
        id=plan.id if plan else None,
        name=plan.name if plan else None,
        price_monthly=plan.price_monthly if plan else None,
        price_yearly=plan.price_yearly if plan else None,
        billing_cycle=org.billing_cycle,
        max_users=plan.max_users if plan else None,
        max_storage_gb=plan.max_storage_gb if plan else None,
        subscription_status=org.subscription_status,
        trial_ends_at=org.trial_ends_at,
        trial_days_left=org.trial_days_left,
        upgrade_status=org.upgrade_status,
    )


def _counts(db: Session, org: Organization, documents: int) -> OverviewCounts:
    in_firm = db.query(func.count(User.id)).filter(User.organization_id == org.id)
    return OverviewCounts(
        employees=in_firm.scalar() or 0,
        active_users=in_firm.filter(User.is_active.is_(True)).scalar() or 0,
        branches=len(org.branch_addresses or []),
        documents=documents,
    )


def _storage(db: Session, org: Organization) -> OverviewStorage:
    files, used = (
        db.query(func.count(StoredFile.id), func.coalesce(func.sum(StoredFile.size), 0))
        .filter(StoredFile.organization_id == org.id)
        .one()
    )
    used = int(used or 0)
    limit_gb = org.plan.max_storage_gb if org.plan is not None else None
    return OverviewStorage(
        files=files or 0,
        used_bytes=used,
        used_mb=round(used / _BYTES_PER_MB, 2),
        used_gb=round(used / _BYTES_PER_GB, 3),
        limit_gb=limit_gb,
        # An unlimited plan has no bar to fill, so it reports no percentage.
        percent_used=(
            round(min(used / (limit_gb * _BYTES_PER_GB) * 100, 100), 1)
            if limit_gb else None
        ),
    )


def _profile_completion(org: Organization, required: list[str]) -> OverviewProfileCompletion:
    missing = [f for f in required if not getattr(org, f, None)]
    filled = len(required) - len(missing)
    return OverviewProfileCompletion(
        percent=round(filled / len(required) * 100) if required else 100,
        filled=filled,
        total=len(required),
        is_complete=not missing,
        missing_information=[FIELD_LABELS.get(f, f.replace("_", " ").title()) for f in missing],
        missing_fields=missing,
    )


def _authorized_person(org: Organization) -> OverviewAuthorizedPerson:
    return OverviewAuthorizedPerson(
        name=org.auth_person_name,
        designation=org.auth_person_designation,
        email=org.auth_person_email,
        mobile=org.auth_person_mobile,
        photo_url=org.auth_person_photo_url,
        signature_url=org.auth_person_signature_url,
        is_complete=all((
            org.auth_person_name, org.auth_person_designation,
            org.auth_person_email, org.auth_person_mobile,
        )),
    )


def _documents(org: Organization) -> OverviewDocuments:
    items = [
        OverviewDocument(
            key=key,
            name=name,
            status="uploaded" if getattr(org, column, None) else "pending",
            url=getattr(org, column, None),
        )
        for key, name, column in DOCUMENT_SLOTS
    ]
    # "Other Business Documents" is the one multi-file slot; each file is its own row.
    for document in org.doc_other_files or []:
        if not isinstance(document, dict):
            continue
        items.append(
            OverviewDocument(
                key="other",
                name=document.get("name") or "Other Document",
                status="uploaded",
                url=document.get("url"),
                size=document.get("size"),
                uploaded_at=document.get("uploaded_at"),
            )
        )
    uploaded = sum(1 for i in items if i.status == "uploaded")
    return OverviewDocuments(
        total=len(items), uploaded=uploaded, pending=len(items) - uploaded, items=items
    )


def _addresses(org: Organization) -> list[OverviewAddress]:
    addresses = [
        OverviewAddress(
            type="registered_office",
            label="Registered Office",
            is_primary=True,
            address=org.registered_address or org.address,
            city=org.city,
            state=org.state,
            country=org.country,
            pin_code=org.pin_code or org.pin_zip_code,
            latitude=org.maps_latitude,
            longitude=org.maps_longitude,
        )
    ]
    for branch in org.branch_addresses or []:
        if not isinstance(branch, dict):
            continue
        addresses.append(
            OverviewAddress(
                id=branch.get("id"),
                type="branch",
                label=branch.get("label") or "Branch",
                address=branch.get("address"),
                city=branch.get("city"),
                state=branch.get("state"),
                country=branch.get("country"),
                pin_code=branch.get("pin_code"),
                latitude=branch.get("latitude"),
                longitude=branch.get("longitude"),
            )
        )
    return addresses


def _activity(entries: list[ActivityLog]) -> list[OverviewActivity]:
    return [
        OverviewActivity(
            id=e.id, type=e.type, title=e.title, description=e.description,
            at=e.created_at, by=e.actor_name,
        )
        for e in entries
    ]


def build_overview(
    db: Session, org: Organization, required_fields: list[str], activity_limit: int = 10
) -> CompanyOverviewOut:
    documents = _documents(org)
    return CompanyOverviewOut(
        company=OverviewCompany(
            id=org.id,
            name=org.name,
            company_code=org.company_code,
            legal_name=org.legal_name,
            industry=org.industry,
            company_type=org.business_type,
            registration_date=org.date_of_incorporation,
            company_status=org.company_status,
            logo_url=org.logo_url or org.company_logo,
            gst_number=org.gst_number,
            pan_number=org.pan_number,
            created_at=org.created_at,
            plan=_plan(org),
        ),
        counts=_counts(db, org, documents.uploaded),
        storage=_storage(db, org),
        profile_completion=_profile_completion(org, required_fields),
        authorized_person=_authorized_person(org),
        documents=documents,
        addresses=_addresses(org),
        recent_activity=_activity(activity_service.recent(db, org.id, activity_limit)),
    )
