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
| GET    | `/users/{id}/overview` | Admin    | Staff Detail's operational summary, shaped by the role's workspace |
| POST   | `/users/me/location` | Authenticated | The field app posts its own GPS reading      |
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

## Staff Detail (Admin)

The page is two calls: the static profile, and the live summary beside it.

```
GET /users/{user_id}            → profile, incl. employment_information.role_detail.workspace
GET /users/{user_id}/overview   → operational summary for that workspace
```

Switch the layout on **`role_detail.workspace`**, never on the role's name — a firm
can call its sales role anything. `role_detail` carries `id`, `name`, `workspace`,
`data_scope`, `is_default` and the full `permissions` matrix.

`GET /users/{user_id}/overview?date_from=&date_to=` returns a stable shape whatever
the workspace. Always filled:

| Block | Holds |
|---|---|
| `user_id`, `employee_id`, `name`, `workspace`, `role` | who this is, and which layout to render |
| `period` | the window the series and period figures cover — defaults to the last 7 days |
| `attendance` | today's row: `status` (`checked_in` / `checked_out` / `absent`), the four checkpoints, `active_duration_minutes` |
| `current_location` | the last GPS reading, or `{"available": false}` |
| `summary`, `performance`, `recent_activity` | shaped by the workspace |

Then, by workspace:

- **`sales`** — `summary` has `today_sales`, `orders_today`, `period_sales`,
  `period_orders`, `assigned_customers`; `performance` is a per-day
  `{date, sales_amount, orders}` series; plus the `recent_orders` and
  `assigned_customers` lists.
- **`delivery`** — `summary` has `deliveries_today` split into `completed_today` /
  `pending_today` / `partial_today` / `failed_today`, plus `delivery_value`,
  `amount_collected` and `amount_receivable`; `performance` is
  `{date, deliveries_completed, delivery_amount}`; plus `assigned_deliveries` (the
  open ones, each with `payment_type` and `amount_due`), `delivery_summary`, and
  `vehicle` — the day's open loading.
- **anything else** — a generic `summary`: `orders_created_period`,
  `sales_amount_period`, `assigned_customers`, `days_present_period`.

Blocks that do not apply to a workspace come back **`null`** rather than being
omitted, so the frontend never has to guard for a missing key.

An order counts as this employee's when they raised it, they are its salesperson, or
it is out for their delivery. `amount_collected` is the payments recorded against
their orders — a payment has no "collected by" column, so the order's assignment is
the link.

### Live location

`POST /users/me/location` writes the caller's own position, and nothing else can
write it:

```json
{ "latitude": 28.6315, "longitude": 77.2167,
  "accuracy_meters": 15, "label": "Connaught Place, New Delhi",
  "captured_at": "2026-08-13T11:18:00Z" }
```

`label` is optional and comes from the device — the backend does no reverse
geocoding. Only the latest reading is kept, and until one arrives
`current_location` reports `{"available": false}`. An employee's `work_location` is
the office they are posted to and is **never** used as a live position.

### Not available yet

`visits_today`, `pending_followups`, `last_visit` and `next_followup` report `null`
because there is no visits or follow-ups module. `pod_completed` and `pod_status`
report `null` because proof of delivery is not captured. `vehicle.vehicle_number` is
null — a loading records who is out with stock, not which van. There is deliberately
no route / stops / view-route data: that needs a real route-planning module.

## Roles, permissions & staff login

An Admin defines roles on the Roles page; staff hold a `role_id` and nothing else.
Permissions are never copied onto a user, so editing a role changes what every
holder can do on their next request.

| Method | Path                | Who   | Purpose                                        |
|--------|---------------------|-------|------------------------------------------------|
| GET    | `/roles/catalog`    | Admin | Every module + action, for the matrix UI        |
| POST   | `/roles`            | Admin | Create a role with its permission matrix       |
| GET    | `/roles`            | Admin | This organization's roles (never another firm's) |
| GET    | `/roles/{id}`       | Admin | Full details + matrix — the Edit Role screen    |
| PATCH  | `/roles/{id}`       | Admin | Update name / workspace / description / scope / permissions |
| DELETE | `/roles/{id}`       | Admin | Custom roles only, and only when unassigned     |

A role carries:

- **`permissions`** — `{ "<module>": { "view": …, "create": …, "edit": …, "delete": …,
  "approve": …, "export": …, "download": … } }`. Deny-by-default: a module with
  nothing granted is dropped rather than stored as all-false. Friendly module names
  are accepted (`orders` → `sales_orders`); `GET /roles/catalog` lists the canonical
  keys. `PATCH` replaces the whole matrix, which is what the Edit screen holds.
- **`workspace`** — free text (`sales` / `delivery` / `accounts` / …). The frontend
  routes on it after login; the backend never interprets it.
- **`data_scope`** — `all` (default, back-office roles see the whole firm) or `own`
  (field roles see only their own records; see below).

The seeded defaults are Sales Officer (`sales`, `own`), Delivery Partner
(`delivery`, `own`) and Accountant (`accounts`, `all`). They can be edited but not
deleted.

### Login

`POST /auth/login` only checks the credentials and returns tokens. The frontend then
calls `GET /auth/me`, which is the single source of truth for the session:

```json
{
  "id": "user_101", "organization_id": "org_abc", "name": "Sunil Sharma",
  "role": { "id": "role_001", "name": "Sales Officer", "workspace": "sales",
            "data_scope": "own", "is_default": true },
  "permissions": { "customers": { "view": true, "create": true, "edit": true,
                                  "delete": false, "approve": false,
                                  "export": false, "download": false } },
  "full_access": false,
  "data_scope": "own",
  "user": { … }, "organization": { … }
}
```

An Admin gets `role: null`, `full_access: true` and a **fully granted** matrix, so
the frontend can read `permissions` the same way for every kind of user.

### Two layers of filtering, both server-side

1. **Organization** — every endpoint scopes to the authenticated user's
   `organization_id`. A client never sends one and cannot widen it. Reaching another
   firm's record is a `404`.
2. **Ownership** — when the role's `data_scope` is `own`, list endpoints return only
   the user's own records, and a record outside their scope reads as `404` (not
   `403`, so they cannot probe which ids exist):

   | Endpoint       | "Mine" means                                              |
   |----------------|-----------------------------------------------------------|
   | `/customers`   | I am the assigned sales representative                     |
   | `/orders`      | I raised it, I am its salesperson, or it is out for my delivery |
   | `/leads`       | assigned to me                                             |
   | `/quotations`  | I am the salesperson                                       |
   | `/deliveries`  | assigned to me                                             |
   | `/attendance`  | my own records                                             |

   A record a scoped user creates is assigned to them automatically — otherwise they
   could not see what they just added. `/products` and `/inventory` are firm-wide
   for anyone with `view`. There is no visits or follow-ups module yet, so nothing
   is scoped for those. `app/core/scoping.py` is the whole mechanism.

Permissions are enforced on the API, not just hidden in the UI: `customers.delete
= false` makes `DELETE /customers/{id}` return `403` regardless of what the
frontend renders.

## Sales flow: settings, warehouse stock, reservations

**Phase 0 of the transaction-flow rework.** The flow is driven by per-organization
settings, never by role names, and an Admin is not a step in every sale.

### Workflow settings

```
GET   /sales-workflow-settings      PATCH /sales-workflow-settings     (Admin)
GET   /invoice-settings             PATCH /invoice-settings            (Admin)
```

Partial updates; anything never set reads back as the documented default, so a
setting added later needs no migration. Both always belong to the caller's firm.

| Setting | Default | Effect |
|---|---|---|
| `order_requires_approval` | **false** | Off: an order is validated, reserved and **placed** on creation. On: it lands in `awaiting_approval` and `/approve` + `/reject` apply — for that firm only |
| `reserve_stock_on_order` | true | Hold stock when placed; on-hand only drops when a vehicle is loaded |
| `allow_backorder` | false | Refuse an order the warehouse cannot cover |
| `credit_limit_action` | `warn` | `warn` returns a `warnings[]` entry, `block` refuses with 400, `ignore` says nothing |
| `invoice_timing` | `after_delivery` | `after_delivery` \| `on_order` |
| `partial_delivery_invoice_mode` | `per_delivery` | `per_delivery` \| `after_full_order` |
| `allow_partial_delivery`, `allow_direct_invoice`, `delivery_collection_allowed` | true | — |

`/invoice-settings` holds `template` (classic / modern / compact / thermal),
`paper_size`, `branding` (`logo_file_id` from `POST /files/upload`, `primary_color`),
15 show/hide `fields`, `terms`, `footer_text` and `notes`. `branding` and `fields`
merge key by key, so one toggle can be flipped without resending the rest. There is
deliberately no separate logo endpoint.

### Warehouses and stock

```
GET  /warehouses            POST /warehouses
GET  /warehouses/{id}       PATCH /warehouses/{id}      DELETE /warehouses/{id}
GET  /warehouses/stock      ?warehouse_id&product_id&low_stock_only
POST /warehouses/{id}/stock/adjust
```

Gated by the `inventory` permission, so a warehouse or dispatch role can manage stock
without being an Admin. Every firm's default warehouse is created on first read, and
an item no warehouse tracks yet opens from its product's own `total_inventory`, so a
firm that never opens a warehouse screen still sees correct figures. The legacy
per-product / per-variant counters are kept equal to the sum across warehouses, so
every existing product, inventory and report endpoint reports the same number it
always did.

The rule the whole flow rests on:

```
available = on_hand − outstanding reservations
```

`on_hand` moves only on a real goods movement. A manual adjustment is refused if it
would take stock below what is already reserved for open orders.

### Orders: two status axes

`status` is the order's own lifecycle, `fulfilment_status` is how far the goods have
got. Payment and invoicing are in neither — a delivered, invoiced, unpaid order is
normal for a credit customer.

```
status            draft | placed | awaiting_approval | processing | completed | cancelled
fulfilment_status not_started | reserved | planned | loaded | in_transit
                  | partially_delivered | delivered | failed
```

Existing rows were migrated from the old single vocabulary on startup, and
`?status=` still accepts the old values (`pending`, `confirmed`, `out_for_delivery`, …)
through the same map — nothing broke on the day of the split. `?fulfilment_status=`
filters the goods side.

### What placing an order now does

```
Validate customer, products, warehouse
  → check available stock
  → create order + items (each snapshotting its own tax_rate)
  → reserve stock
  → status = placed, fulfilment_status = reserved
```

It does **not** deduct warehouse stock, **not** create a receivable, and **not** wait
for an Admin. `POST /orders` accepts `warehouse_id`, `delivery_date`,
`fulfilment_method`, `payment_type`, `payment_terms_days` and `quotation_id`, and
returns `stock_summary` (on hand / reserved / available) plus any `warnings`.

A shortage is caught when the order is placed, with the detail naming it:

```json
{ "error": "INSUFFICIENT_STOCK",
  "shortages": [{ "product_id": "…", "product_name": "…",
                  "required_quantity": 20, "available_quantity": 12,
                  "short_quantity": 8 }] }
```

Lines for the same item are summed before the check, so two lines cannot each pass
against the full availability.

Other corrections in this phase:

- **Cancelling** releases the reservations. Physical stock is untouched and no fake
  stock-in movement is invented. Once goods are loaded or dispatched a plain cancel
  is refused — that is the delivery-return flow.
- **Assigning a delivery partner** plans the delivery (`fulfilment_status: planned`).
  It no longer jumps straight to out-for-delivery.
- **The receivable starts at the invoice**, not at the order.
- **The hardcoded 18% is gone.** An invoice bills the `tax_rate` snapshotted on the
  order line, which comes from the line or the product's own rate. Products carry a
  `tax_rate` field for this.

Still to come in later phases: quotation lifecycle and conversion, the unified
Delivery record with Delivery ID as the identifier, vehicle loading against delivery
items, challan PDF, dispatch, POD, invoice from delivered quantity, simple/detailed
PDF formats, quick billing, ledger + ageing, the sales-return rework, and
batch / serial tracking.

## Admin dashboard

`GET /dashboard/admin` (needs `dashboard.view`) returns every widget on the Admin
Dashboard in one call, for the logged-in user's firm — no `organization_id` is
accepted.

Query params: `date_from`, `date_to` (YYYY-MM-DD; default the 1st of this month →
today), plus optional `branch_id`, `warehouse_id`, `customer_id`, `supplier_id`.
A filter naming something outside the firm is a `400` rather than a silently empty
dashboard.

Blocks: `filters`, `summary`, `orders`, `cashflow`, `receivables_payables`,
`top_customers`, `top_products`, `expense_breakdown`, `sales_trend`, `stock_watch`,
`recent_orders`. Summaries only — `GET /reports/{type}` and the module list
endpoints still serve the drill-in screens.

Definitions match the reports, so a dashboard figure equals the report behind it:
a sale is an order past approval, purchases and expenses count only approved ones,
gross profit is sales − purchases and net is gross − expenses (there is no cost
price on a product yet, so no COGS-based margin). `receivables_payables` and
`stock_watch` are a position as of now, so the date range does not apply to them;
"overdue" uses the customer's `payment_terms` (`net_30` → 30 days) where set, else
30 days. `branch_id` is validated against the firm's branches but no transaction
carries a branch yet, so it does not narrow the figures.

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
