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


class SystemRole(str, enum.Enum):
    """Top-level role for routing/access control (checked before the permission matrix)."""

    SUPER_ADMIN = "super_admin"
    ADMIN = "admin"
    STAFF = "staff"


# Map the legacy UserRole enum to a system role.
def system_role_for(role: "UserRole | None") -> str:
    if role == UserRole.SUPER_ADMIN:
        return SystemRole.SUPER_ADMIN.value
    if role == UserRole.ADMIN:
        return SystemRole.ADMIN.value
    return SystemRole.STAFF.value


# Legacy staff UserRole -> the matching default role name (for backfill / legacy create).
STAFF_ROLE_NAME = {
    UserRole.ACCOUNTANT: "Accountant",
    UserRole.SALES_OFFICER: "Sales Officer",
    UserRole.DELIVERY_PARTNER: "Delivery Partner",
}
# Inverse: default role name -> legacy UserRole (None for custom roles).
LEGACY_ROLE_BY_NAME = {name: role for role, name in STAFF_ROLE_NAME.items()}


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


class BillingCycle(str, enum.Enum):
    MONTHLY = "monthly"
    YEARLY = "yearly"


# Statuses where data-mutation endpoints are blocked (read-only + upgrade allowed).
LOCKED_STATUSES = {OrganizationStatus.LOCKED, OrganizationStatus.SUSPENDED, OrganizationStatus.INACTIVE}
