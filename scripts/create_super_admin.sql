-- Creates the platform Super Admin directly in Postgres (for pgAdmin / psql).
-- Equivalent of `seed_super_admin()` in app/seed.py — safe to re-run, it will
-- just report 0 rows inserted if the email already exists (ON CONFLICT DO NOTHING
-- would need a unique index on email alone, which doesn't exist since email is
-- only unique per-organization; the WHERE NOT EXISTS guard below covers it instead).
--
-- Password hash below is bcrypt for: Admin@123
-- To use a different password, generate your own hash and replace it, e.g.:
--   python -c "import bcrypt; print(bcrypt.hashpw(b'YourNewPassword', bcrypt.gensalt()).decode())"

INSERT INTO users (
    id,
    organization_id,
    name,
    email,
    password_hash,
    role,
    system_role,
    is_active,
    created_at
)
SELECT
    gen_random_uuid()::text,   -- requires pgcrypto; see fallback note below
    NULL,                      -- platform-level user, no tenant
    'Ravi Malhotra',
    'superadmin@demo.com',
    '$2b$12$K8J0qDrFolcjYzHnI9hP9.j3hroDcMsxSdwXGVEpzAM9.kxXUfy0.',
    'SUPER_ADMIN',
    'super_admin',
    true,
    now()
WHERE NOT EXISTS (
    SELECT 1 FROM users WHERE email = 'superadmin@demo.com'
);

-- If gen_random_uuid() errors with "function does not exist", either run once:
--   CREATE EXTENSION IF NOT EXISTS pgcrypto;
-- or replace gen_random_uuid()::text above with: md5(random()::text || clock_timestamp()::text)
