-- Pro plan: max_users 10 -> 5 (meeting decision: Basic 1, Pro 5, Enterprise unlimited).
--
-- NOT LIVE. alembic/pending/ is not scanned by Alembic and nothing runs this file
-- automatically. Run it by hand on production only after review and a backup, or make the
-- same change from the Super Admin plan screen instead.
--
-- Data only: no schema change, no lock beyond one row. max_storage_gb is not touched.
--
-- Existing firms are grandfathered: the limit is checked only when a new user is added
-- (POST /users: blocked when active users >= limit). A Pro firm that already has 6-10
-- active users keeps every one of them, can log in and work as before, and just cannot add
-- another user until it is below 5 active users. No user is deactivated by this change.
-- A firm-level limit override (organization_limit_overrides, max_users) still wins over
-- the plan value, so a firm can be given more seats without changing the plan.

-- 1. Preview: Pro firms that are already above 5 active users (they keep their users).
SELECT o.id, o.name, COUNT(u.id) AS active_users
FROM organizations o
JOIN plans p ON p.id = o.plan_id AND p.name = 'Pro'
JOIN users u ON u.organization_id = o.id AND u.is_active = TRUE
GROUP BY o.id, o.name
HAVING COUNT(u.id) > 5
ORDER BY active_users DESC;

-- 2. The change (only if the row still has the old value).
UPDATE plans SET max_users = 5 WHERE name = 'Pro' AND (max_users IS NULL OR max_users > 5);

-- 3. Check.
SELECT name, max_users, max_storage_gb FROM plans ORDER BY name;

-- Rollback: UPDATE plans SET max_users = 10 WHERE name = 'Pro';
