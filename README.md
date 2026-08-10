# CRM SaaS — Backend

FastAPI backend for the CRM / Billing / Inventory SaaS. This first slice covers
**authentication, multi-tenant organizations, and user management**.

## Stack

- **FastAPI** + **Uvicorn** — JSON API + auto OpenAPI docs
- **SQLAlchemy 2.0** — ORM (SQLite for dev, Postgres for production)
- **PyJWT** + **bcrypt** — JWT access/refresh tokens, password hashing

## Multi-tenancy model

- **Super Admin** — runs the platform (organizations, plans). Not tied to any firm.
- **Organization** — a firm/shop (the tenant). All firm data is scoped to it.
- **Admin** — owns a firm. Self-registers, then creates staff under the firm.
- **Staff** — `accountant`, `sales_officer`, `delivery_partner`. Created by the Admin only.

## How to run

**Prerequisite:** Python 3.11+ installed. Nothing else — no database, Docker, or
cloud account needed. It uses a local SQLite file that is created automatically.

### Windows (PowerShell)

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env            # optional; sensible defaults exist
python -m app.seed                # creates Super Admin + demo firm
uvicorn app.main:app --reload
```

### macOS / Linux

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # optional; sensible defaults exist
python -m app.seed                # creates Super Admin + demo firm
uvicorn app.main:app --reload
```

### 👉 Open the API in your browser (Swagger UI)

Once the server is running, open:

- **Swagger UI (interactive docs + test the API):** http://127.0.0.1:8000/docs
- ReDoc (read-only reference): http://127.0.0.1:8000/redoc
- Raw OpenAPI spec (import into Postman): http://127.0.0.1:8000/openapi.json

**Test a protected endpoint in Swagger:**
1. `POST /auth/login` → *Try it out* → use a seeded login below → **Execute** →
   copy `tokens.access_token` from the response.
2. Click the green **Authorize** 🔓 button (top-right) → paste the token → *Authorize*.
3. Now every 🔒 endpoint (`/auth/me`, `/users`, …) works.

> Note: `--reload` is for development only. To run without auto-reload use
> `uvicorn app.main:app --host 0.0.0.0 --port 8000`.

### Seeded logins (dev)

| Role        | Email                | Password   |
|-------------|----------------------|------------|
| Super Admin | superadmin@demo.com  | Admin@123  |
| Admin       | admin@demo.com       | Admin@123  |

## Auth endpoints

| Method | Path             | Who            | Purpose                                   |
|--------|------------------|----------------|-------------------------------------------|
| POST   | `/auth/register` | Public         | Admin self-registers a new firm + account |
| POST   | `/auth/login`    | Public         | Email + password → access/refresh tokens  |
| POST   | `/auth/refresh`  | Public         | Refresh token → new access token          |
| POST   | `/auth/logout`   | Public         | Revoke a refresh token                     |
| GET    | `/auth/me`       | Authenticated  | Current user profile                       |
| POST   | `/auth/forgot-password` | Public  | Email a reset link (always 200, no leak)   |
| POST   | `/auth/reset-password`  | Public  | Set a new password using the emailed token |
| POST   | `/auth/change-password` | Authenticated | Change own password (needs current)  |

Send the access token as `Authorization: Bearer <token>`.

## Employee endpoints

The whole employee module, deliberately kept to this. Anything that used to have
an endpoint of its own — status changes, role reassignment, per-field uploads,
dropdown data — is part of `PATCH /users/{id}` or gone.

| Method | Path             | Who            | Purpose                                   |
|--------|------------------|----------------|-------------------------------------------|
| POST   | `/users`         | Admin          | Create an employee (sectioned body)        |
| GET    | `/users`         | Admin          | List/search employees (role, status, designation, employment type) |
| GET    | `/users/{id}`    | Admin          | One employee's full sectioned profile      |
| PATCH  | `/users/{id}`    | Admin          | Edit anything about an employee            |
| DELETE | `/users/{id}`    | Admin          | Permanently remove an employee             |
| DELETE | `/users/{id}/documents/{collection}?document_id=` | Admin | Drop one file out of a document list |
| POST   | `/users/{id}/reset-password` | Admin | Admin sets a staff member's password    |
| POST   | `/files/upload`  | Authenticated  | Upload any file → `{file_id, url, name, …}` |
| GET    | `/files/{id}`    | Public         | Serve an uploaded file (capability URL)     |
| DELETE | `/files/{id}`    | Authenticated  | Discard an uploaded file                   |

Removed, and what replaces each:

| Gone                                    | Use instead                                        |
|-----------------------------------------|----------------------------------------------------|
| `PATCH /users/{id}/status`               | `system_preferences.account_status` on `PATCH /users/{id}` |
| `PATCH /users/{id}/account-status`       | same                                                |
| `PATCH /users/{id}/role`                 | `employment_information.role_id` (or `role` by name) |
| `POST /users/{id}/identity-proof`        | `POST /files/upload` → `documents.identity_proof_file` |
| `POST /users/{id}/files/{field}`         | `POST /files/upload` → the matching field           |
| `DELETE /users/{id}/files/{field}`       | send that field as `null`                           |
| `GET/POST /users/{id}/documents/{collection}` | the `documents` section on `PATCH /users/{id}` |
| `GET /users/meta/employee-options`       | nothing — the frontend owns its dropdowns           |

### How the employee body works

A user *is* the employee record. `POST /users` and `PATCH /users/{id}` take the
same **sectioned** body — `basic_information`, `contact_information`,
`address_information`, `employment_information`, `login_security`,
`payroll_information`, `documents`, `professional_information`,
`system_preferences` — and `GET /users/{id}` returns it in that shape. The table
underneath stays flat; `app/schemas/employee_profile.py` holds the mapping. Older
flat bodies (`{"first_name": …, "email": …}`) are still folded into their sections,
and `GET /users` (the list) stays flat, one row per employee.

- **Nothing is required** but the two a login account cannot exist without:
  `contact_information.official_email` and `login_security.password`. Which fields
  a form insists on is the frontend's business, and no dropdown data is served
  from here.
- **One endpoint for every change.** There is no separate status API: an employee
  resigning is `employment_information` (`employee_status`, `date_of_exit`) plus
  `system_preferences.account_status` in a single `PATCH`. `account_status` drives
  `is_active`, so only an `active` account can log in; `employee_status` is the HR
  lifecycle. `login_security.password` and `employment_information.role_id` are
  accepted on `PATCH` too.
- **Files are uploaded before the employee exists.** `POST /files/upload` takes no
  record id and returns `{file_id, url, name, content_type, size}`; put the `url`
  into `basic_information.profile_photo` or the `documents` section on create. The
  document lists (`experience_certificates`, `educational_certificates`,
  `other_documents`) are URL lists in and out. Drop one file with
  `DELETE /users/{id}/documents/{collection}/{document_id}`, where `document_id`
  is the `file_id` (the last segment of the URL); empty a named slot with
  `DELETE /users/{id}/files/{field}`, and discard an upload that was never
  attached with `DELETE /files/{id}`.
- `employee_id` is a per-firm series. Omit it and the next free `EMP-0001`,
  `EMP-0002`, … is assigned; supplying one that's already used returns `409`.
- `employment_type` ∈ `full_time | part_time | contract | intern | temporary` and
  `employee_status` ∈ `active | probation | on_leave | notice_period | resigned |
  terminated`. Input is normalized, so `"Full Time"` and `"Full-time"` both work.
- `DELETE /users/{id}` is a hard delete: the user's own rows (attendance,
  notifications, sessions) go with them, while records that merely reference them
  (customers, leads, quotations, sales orders) survive with the link nulled out.
  You can't delete yourself, and a delivery partner with an open vehicle loading
  returns `409` until the end-of-day is recorded. Set `account_status` to
  `inactive` instead when the history should stay attributed.

## Company Settings dashboard

`GET /organizations/overview` (Admin) is the one call behind the Company Settings
page. Read-only and entirely derived — one block per card:

| Block                 | What it holds                                                       |
|-----------------------|---------------------------------------------------------------------|
| `company`             | Name, `company_code` (`CMP-10001`, issued on first read), legal name, industry, `company_type` (from `business_type`), `registration_date` (from `date_of_incorporation`), `company_status`, logo, GST/PAN, plus a nested `plan` with the subscription state, trial countdown and quotas |
| `counts`              | `employees`, `active_users`, `branches`, `documents`                 |
| `storage`             | `files`, `used_bytes` / `used_mb` / `used_gb`, `limit_gb` from the plan's `max_storage_gb` (null = unlimited) and `percent_used` |
| `profile_completion`  | `percent`, `filled`/`total`, and the gaps both as display labels (`missing_information`) and as column names (`missing_fields`) |
| `authorized_person`   | Name, designation, email, mobile, photo, signature, `is_complete`    |
| `documents`           | `uploaded`/`pending` counts plus one row per document — the six named slots and every "other" file, each with `key`, `name`, `status`, `url` |
| `addresses`           | The registered office first (`is_primary`), then each branch, all with `latitude`/`longitude` |
| `recent_activity`     | Newest first: `type`, `title`, `description`, `at`, `by`. `?activity_limit=` (1–100, default 10) sizes it — that is what the page's "View All" reads |

Recent Activity comes from the `activity_logs` table, written by
`activity_service.record()` inside the transaction that made the change. Company
profile updates are split by the section of the page they touched, so one `PUT
/settings` can produce "Billing information changed" and "Authorized person
updated" as separate entries. Employee create / update / delete are recorded too.
Dropping `activity_service.record(...)` into another router adds it to the feed.

Two things the page shows that the backend does not store yet: document **expiry**
(`expires_at` is always null, so nothing reports `expired`) and any notion of the
authorized person being externally *verified* — `is_complete` only says every field
is filled in.

## Password reset (SMTP)

- `POST /auth/forgot-password` generates a single-use, 30-min token and emails a
  link to `FRONTEND_RESET_URL?token=...`. The response is always the same so it
  never reveals whether an email is registered.
- Leave `SMTP_HOST` empty in dev — emails are **printed to the server log** instead
  of sent. Set `EXPOSE_RESET_TOKEN=true` to also get the raw token in the API
  response for testing. **Both are dev-only; disable in production.**
- Staff who forget their password can also be reset by their Admin via
  `POST /users/{id}/reset-password` — no email needed.
- Any password change/reset revokes the user's refresh tokens (forces re-login).

## Tests

```powershell
.\.venv\Scripts\python.exe test_smoke.py   # 37 end-to-end checks, uses a throwaway DB
```

## Deploy to Render

This repo ships a `render.yaml` Blueprint that provisions the **API + a free
PostgreSQL database** together. SQLite is dev-only — Render's disk is ephemeral,
so production uses Postgres (the app auto-switches based on `DATABASE_URL`).

**Steps:**
1. Push this repo to GitHub (done: `harshngit/saasbackend`).
2. In Render: **New + → Blueprint** → connect this repo → Render reads `render.yaml`.
3. When prompted, set the values marked `sync: false`:
   - `SUPER_ADMIN_PASSWORD` — a strong password for the platform owner.
   - SMTP fields (`SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD`, `FRONTEND_RESET_URL`) —
     optional; leave blank to just log reset emails.
4. **Apply** → Render builds the web service, creates the Postgres DB, injects
   `DATABASE_URL`, generates `JWT_SECRET`, and seeds the Super Admin on first boot.
5. Open `https://<your-service>.onrender.com/docs`.

**Good to know (free tier):**
- The web service **sleeps after ~15 min idle**; the first request then takes
  ~50s to wake (cold start).
- Free Postgres is **removed after 30 days** — fine for testing, upgrade for real use.
- After the app is live, tighten `CORS_ORIGINS` from `*` to your real front-end URL,
  and change `SEED_ON_STARTUP` to `false` once the Super Admin exists.

## Going to production (hardening)

1. `DATABASE_URL` → managed Postgres (Render Blueprint does this automatically).
   `postgres://` / `postgresql://` URLs are auto-rewritten to psycopg3.
2. Strong `JWT_SECRET` (Blueprint generates one; or
   `python -c "import secrets; print(secrets.token_hex(32))"`).
3. Replace startup `create_all` with **Alembic** migrations before schema changes.
4. Restrict `CORS_ORIGINS` to your real front-end origins.
5. Turn `SEED_ON_STARTUP` off after the first deploy.
