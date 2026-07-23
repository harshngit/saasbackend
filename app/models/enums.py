import enum


class UserRole(str, enum.Enum):
    """Platform + firm-level roles.

    SUPER_ADMIN runs the SaaS platform (manages organizations/plans) and is not
    tied to any organization. ADMIN owns a firm and creates the staff roles
    below under that firm.
    """

    SUPER_ADMIN = "super_admin"
    ADMIN = "admin"
    ACCOUNTANT = "accountant"
    SALES_OFFICER = "sales_officer"
    DELIVERY_PARTNER = "delivery_partner"


# Roles an Admin is allowed to create for staff within their own firm.
STAFF_ROLES = {UserRole.ACCOUNTANT, UserRole.SALES_OFFICER, UserRole.DELIVERY_PARTNER}


class OrganizationStatus(str, enum.Enum):
    TRIAL = "trial"
    ACTIVE = "active"
    LOCKED = "locked"        # trial expired; data-mutation blocked until upgrade
    INACTIVE = "inactive"
    SUSPENDED = "suspended"


class PlanTier(str, enum.Enum):
    FREE = "free"
    BASIC = "basic"
    PRO = "pro"
    ENTERPRISE = "enterprise"


class UpgradeStatus(str, enum.Enum):
    NONE = "none"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


# Statuses where data-mutation endpoints are blocked (read-only + upgrade allowed).
LOCKED_STATUSES = {OrganizationStatus.LOCKED, OrganizationStatus.SUSPENDED, OrganizationStatus.INACTIVE}
