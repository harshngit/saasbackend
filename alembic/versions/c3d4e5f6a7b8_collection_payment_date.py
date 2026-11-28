"""collection_payment_date: keep payment_date (business day) apart from collected_at

NOT LIVE YET. This file sits in alembic/pending/, which Alembic does not scan. To release it,
after the database backup: move it to alembic/versions/ and set down_revision to the current
head (`alembic heads`); chain it after any other pending file released at the same time.
The app works before and after: until the column exists it is neither read nor written
(app/services/collection_dates.py checks for it).

1. Adds delivery_collections.payment_date (DATE, nullable). Adding a nullable column with no
   default is a metadata-only change in Postgres (no table rewrite).
2. Fills payment_date for every existing row:
   - collected_at exactly 00:00:00 UTC (an old date-only payment_date): that UTC date, which
     is the date the user picked;
   - otherwise: collected_at's date in the firm's timezone (default Asia/Kolkata).
3. Fixes collected_at ONLY where it is exactly midnight UTC AND created_at falls on that same
   date (firm timezone): collected_at = created_at. Rows where the dates differ are left as
   they are (they still get payment_date). Nothing is deleted.

Report before releasing (read-only):
    SELECT count(*) AS midnight_rows,
           count(*) FILTER (WHERE same_day) AS safe_to_backfill,
           count(*) FILTER (WHERE NOT same_day) AS skipped_dates_differ
    FROM (
        SELECT (dc.created_at AT TIME ZONE COALESCE(NULLIF(o.timezone, ''), 'Asia/Kolkata'))::date
               = (dc.collected_at AT TIME ZONE 'UTC')::date AS same_day
        FROM delivery_collections dc JOIN organizations o ON o.id = dc.organization_id
        WHERE (dc.collected_at AT TIME ZONE 'UTC')::time = '00:00:00'
    ) t;
    -- the skipped rows themselves:
    SELECT dc.id, dc.organization_id, dc.collected_at, dc.created_at, dc.reconciliation_status
    FROM delivery_collections dc JOIN organizations o ON o.id = dc.organization_id
    WHERE (dc.collected_at AT TIME ZONE 'UTC')::time = '00:00:00'
      AND (dc.created_at AT TIME ZONE COALESCE(NULLIF(o.timezone, ''), 'Asia/Kolkata'))::date
          <> (dc.collected_at AT TIME ZONE 'UTC')::date;

Downgrade drops the column; the collected_at fixes in step 3 are not reverted.

Revision ID: c3d4e5f6a7b8
Revises: e7f1a2b3c4d5
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, None] = "e7f1a2b3c4d5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TZ = "COALESCE(NULLIF(o.timezone, ''), 'Asia/Kolkata')"


def upgrade() -> None:
    op.add_column("delivery_collections", sa.Column("payment_date", sa.Date(), nullable=True))
    op.execute(
        f"""
        UPDATE delivery_collections AS dc
        SET payment_date = CASE
                WHEN (dc.collected_at AT TIME ZONE 'UTC')::time = '00:00:00'
                    THEN (dc.collected_at AT TIME ZONE 'UTC')::date
                ELSE (dc.collected_at AT TIME ZONE {_TZ})::date
            END
        FROM organizations AS o
        WHERE o.id = dc.organization_id AND dc.payment_date IS NULL
        """
    )
    op.execute(
        f"""
        UPDATE delivery_collections AS dc
        SET collected_at = dc.created_at
        FROM organizations AS o
        WHERE o.id = dc.organization_id
          AND (dc.collected_at AT TIME ZONE 'UTC')::time = '00:00:00'
          AND (dc.created_at AT TIME ZONE {_TZ})::date = (dc.collected_at AT TIME ZONE 'UTC')::date
        """
    )


def downgrade() -> None:
    op.drop_column("delivery_collections", "payment_date")
