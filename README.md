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
| POST   | `/users`         | Admin          | Create a staff user in the firm            |
| GET    | `/users`         | Admin          | List users in the firm                     |
| PATCH  | `/users/{id}/status` | Admin      | Activate / deactivate a firm user          |
| POST   | `/users/{id}/reset-password` | Admin | Admin sets a staff member's password    |

Send the access token as `Authorization: Bearer <token>`.

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

## Going to production

1. Point `DATABASE_URL` at Postgres, e.g.
   `postgresql+psycopg://user:pass@host:5432/crm_saas`
2. Set a strong `JWT_SECRET` (`python -c "import secrets; print(secrets.token_hex(32))"`).
3. Replace startup `create_all` with **Alembic** migrations.
4. Restrict `CORS_ORIGINS` to your real front-end origins.
