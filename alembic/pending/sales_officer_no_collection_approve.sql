-- Default "Sales Officer" role: no Confirm Payment / Void on collections.
--
-- NOT LIVE. alembic/pending/ is not scanned by Alembic and nothing runs this file
-- automatically. Run it by hand on production only after review and a backup.
--
-- Why: Confirm (reconcile) and Void need the role's deliveries.approve right. The seeded
-- Sales Officer matrix has never had it (app/core/permissions.py), and seeding never rewrites
-- an existing role, so a Sales Officer that can Confirm/Void got deliveries.approve from an
-- edit on the Roles page (or an older custom matrix). This removes only that one right, only
-- on the firm's default Sales Officer role (is_default = true). Custom roles, Admin,
-- Accountant and Delivery Partner rows are not touched. deliveries.approve also gates
-- Day-End approval, so those Sales Officers lose that too (intended: back-office right).
--
-- 1. Check first (read-only): which firms' default Sales Officer can approve today.
SELECT r.organization_id, r.id AS role_id, r.name, r.permissions -> 'deliveries' AS deliveries_perms
FROM roles r
WHERE r.is_default = true
  AND lower(replace(replace(r.name, '_', ' '), '-', ' ')) = 'sales officer'
  AND (r.permissions::jsonb -> 'deliveries' ->> 'approve')::boolean IS TRUE;

-- 2. The change (data only, one UPDATE).
UPDATE roles r
SET permissions = jsonb_set(r.permissions::jsonb, '{deliveries,approve}', 'false'::jsonb)::json
WHERE r.is_default = true
  AND lower(replace(replace(r.name, '_', ' '), '-', ' ')) = 'sales officer'
  AND (r.permissions::jsonb -> 'deliveries' ->> 'approve')::boolean IS TRUE;
