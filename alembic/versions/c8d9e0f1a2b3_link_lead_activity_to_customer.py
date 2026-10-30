"""visits / follow_ups: link lead-logged activity to the Lead's Customer (data only)

A Visit or Follow-up logged on a Lead that already had a Customer (converted, or
pre-linked at creation) was stored with lead_id only, so the Customer's Activities
(GET /customers/{id}/visits, /follow-ups) never showed it. The application now sets
customer_id at write time (lead_service.linked_customer_id); this revision backfills
the rows written before that.

- sets customer_id = leads.customer_id on visits / follow_ups rows that have a lead_id,
  no customer_id, and whose Lead has a Customer in the SAME organization;
- never creates, deletes or duplicates rows, never touches a row that already has a
  customer_id, never changes any Lead's status (a pre-linked Lead is only marked
  converted by POST /leads/{id}/convert-to-customer);
- idempotent; no schema change.

Downgrade is a no-op: the backfilled links are indistinguishable from ones the
application writes itself, and lead_id is preserved on every row.

Revision ID: c8d9e0f1a2b3
Revises: b7c8d9e0f1a2
Create Date: 2026-10-30 12:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c8d9e0f1a2b3"
down_revision: Union[str, Sequence[str], None] = "b7c8d9e0f1a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _backfill(table: str) -> None:
    op.execute(sa.text(f"""
        UPDATE {table}
        SET customer_id = (
            SELECT l.customer_id
            FROM leads l
            JOIN customers c ON c.id = l.customer_id
            WHERE l.id = {table}.lead_id
              AND l.organization_id = {table}.organization_id
              AND c.organization_id = {table}.organization_id
        )
        WHERE customer_id IS NULL
          AND lead_id IS NOT NULL
          AND EXISTS (
            SELECT 1
            FROM leads l
            JOIN customers c ON c.id = l.customer_id
            WHERE l.id = {table}.lead_id
              AND l.organization_id = {table}.organization_id
              AND c.organization_id = {table}.organization_id
          )
    """))


def upgrade() -> None:
    _backfill("visits")
    _backfill("follow_ups")


def downgrade() -> None:
    pass
