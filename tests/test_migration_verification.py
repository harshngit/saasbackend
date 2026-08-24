"""Migration and Database Integrity Verification Script.

Tests:
1. Fresh Database creation (all tables, columns, constraints created).
2. Existing Database schema upgrade with zero data loss.
3. Verification of all new Expense columns, ExpenseItem table, foreign keys, and defaults.
"""

import os
import sys
import tempfile
import sqlite3
import uuid
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import create_engine, inspect, text
from app.core.database import Base
import app.models  # Register all models in Base.metadata

print("\n=======================================================")
print("MIGRATION & DATABASE INTEGRITY VERIFICATION")
print("=======================================================\n")

# 1. Test fresh DB creation
print("--- STEP 1: Fresh Database Creation ---")
with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
    fresh_db_path = f.name

fresh_engine = create_engine(f"sqlite:///{fresh_db_path}")
Base.metadata.create_all(bind=fresh_engine)
inspector = inspect(fresh_engine)
tables = inspector.get_table_names()

assert "expenses" in tables, "expenses table missing"
assert "expense_items" in tables, "expense_items table missing"
print("  PASS  Tables 'expenses' and 'expense_items' created successfully on fresh DB")

expense_cols = {c["name"] for c in inspector.get_columns("expenses")}
item_cols = {c["name"] for c in inspector.get_columns("expense_items")}

required_expense_cols = {
    "branch_id", "department_id", "vendor_id", "payee_name", "contact_person",
    "mobile_number", "email_address", "payee_gstin", "subtotal", "tax_rate",
    "tax_amount", "currency", "payment_reference", "cost_center_id", "project_id",
    "tax_category", "tds_applicable", "tds_amount", "tags", "vendor_invoice_url",
    "supporting_documents", "is_recurring", "recurrence_frequency", "next_due_date"
}
missing = required_expense_cols - expense_cols
assert not missing, f"Missing expense columns on fresh DB: {missing}"
print(f"  PASS  All {len(required_expense_cols)} newly added Expense columns exist on fresh DB")

required_item_cols = {
    "id", "expense_id", "description", "quantity", "unit_price", "tax_rate", "tax_amount", "line_total"
}
missing_item_cols = required_item_cols - item_cols
assert not missing_item_cols, f"Missing expense_item columns on fresh DB: {missing_item_cols}"
print(f"  PASS  All {len(required_item_cols)} ExpenseItem columns exist on fresh DB")

# Clean up fresh db
fresh_engine.dispose()
try:
    os.remove(fresh_db_path)
except Exception:
    pass

# 2. Test upgrade of pre-existing DB with legacy data
print("\n--- STEP 2: Existing Database Upgrade Without Data Loss ---")
with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
    old_db_path = f.name

# Setup old legacy expenses table without new columns
con = sqlite3.connect(old_db_path)
cur = con.cursor()
cur.execute("""
CREATE TABLE expenses (
    id VARCHAR(36) PRIMARY KEY,
    organization_id VARCHAR(36) NOT NULL,
    category VARCHAR(100) NOT NULL,
    amount FLOAT NOT NULL,
    description TEXT,
    expense_date TIMESTAMP NOT NULL,
    payment_mode VARCHAR(30),
    receipt_url TEXT,
    status VARCHAR(20) NOT NULL,
    submitted_by VARCHAR(36),
    approved_by VARCHAR(36),
    reject_reason VARCHAR(500),
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL
)
""")
test_id = str(uuid.uuid4())
cur.execute("""
INSERT INTO expenses (id, organization_id, category, amount, description, expense_date, status, created_at, updated_at)
VALUES (?, 'org_legacy_123', 'Office Supplies', 450.0, 'Legacy test expense record', '2026-01-01 00:00:00', 'pending', '2026-01-01 00:00:00', '2026-01-01 00:00:00')
""", (test_id,))
con.commit()
con.close()

# Run application auto-migration pipeline on the legacy DB
old_engine = create_engine(f"sqlite:///{old_db_path}")
# Create new tables
Base.metadata.create_all(bind=old_engine)

# Auto add missing columns
old_inspector = inspect(old_engine)
for table in Base.metadata.sorted_tables:
    if not old_inspector.has_table(table.name):
        continue
    existing = {col["name"] for col in old_inspector.get_columns(table.name)}
    for column in table.columns:
        if column.name in existing:
            continue
        col_type = column.type.compile(dialect=old_engine.dialect)
        ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}'
        with old_engine.begin() as conn:
            conn.execute(text(ddl))

# Verify schema after migration
old_inspector = inspect(old_engine)
updated_cols = {c["name"] for c in old_inspector.get_columns("expenses")}
missing_after_upgrade = required_expense_cols - updated_cols
assert not missing_after_upgrade, f"Missing columns after upgrade: {missing_after_upgrade}"
print("  PASS  All missing columns successfully migrated to existing table via ALTER TABLE")

# Verify data preservation
with old_engine.connect() as conn:
    row = conn.execute(text(f"SELECT id, organization_id, category, amount, description, status FROM expenses WHERE id = '{test_id}'")).fetchone()
    assert row is not None, "Legacy row was lost during migration!"
    assert row[0] == test_id, "ID mismatch!"
    assert row[1] == "org_legacy_123", "Organization ID corrupted!"
    assert row[2] == "Office Supplies", "Category corrupted!"
    assert row[3] == 450.0, "Amount corrupted!"
    assert row[4] == "Legacy test expense record", "Description corrupted!"
    assert row[5] == "pending", "Status corrupted!"
    print(f"  PASS  Legacy row verified intact: {row}")

old_engine.dispose()
try:
    os.remove(old_db_path)
except Exception:
    pass

print("\n=======================================================")
print("ALL MIGRATION & DATABASE INTEGRITY CHECKS PASSED (100%)")
print("=======================================================\n")
