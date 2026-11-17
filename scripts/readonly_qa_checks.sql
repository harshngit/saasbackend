-- READ-ONLY checks. Every query runs inside a READ ONLY transaction that is rolled back:
-- nothing here can change data. Run with psql against the production database:
--   psql "$DATABASE_URL" -f scripts/readonly_qa_checks.sql
-- (or paste one block at a time into the PlanetScale console).

BEGIN TRANSACTION READ ONLY;

-- ---------------------------------------------------------------------------------------
-- A. Supplier invoices whose date may be wrong (the old `invoice_date` bug).
-- The table has no `invoice_date` column: the API's `invoice_date` is
-- supplier_invoice_date. When the frontend's date was dropped, the row got "now", so
-- supplier_invoice_date is within seconds of created_at. A date that was really sent is
-- stored as midnight, so `likely_defaulted` = true is the strong signal; same_day_only
-- rows (same date, not same moment) are probably genuine same-day invoices.
-- ---------------------------------------------------------------------------------------
SELECT si.id,
       si.supplier_invoice_number,
       s.name                                   AS supplier,
       si.supplier_invoice_date                 AS invoice_date,
       si.supplier_invoice_date,
       si.due_date,
       si.created_at,
       si.status,
       si.organization_id,
       abs(extract(epoch FROM si.supplier_invoice_date - si.created_at)) < 60 AS likely_defaulted
FROM supplier_invoices si
LEFT JOIN suppliers s ON s.id = si.supplier_id
WHERE si.supplier_invoice_date::date = si.created_at::date
ORDER BY likely_defaulted DESC, si.created_at;

-- ---------------------------------------------------------------------------------------
-- B. QA-TEST rows: every row of these tables with "QA-TEST" anywhere in it (any text
-- column), plus rows linked to a QA-TEST supplier / customer / product / purchase.
-- ---------------------------------------------------------------------------------------
-- B1. The list itself. (Each query below repeats the same qa_rows definition:
-- a READ ONLY transaction cannot create a view.)
WITH named AS (
    SELECT 'supplier' AS kind, id, organization_id, name AS label, NULL::text AS status, created_at FROM suppliers t WHERE t::text ILIKE '%QA-TEST%'
    UNION ALL SELECT 'customer', id, organization_id, name, NULL, created_at FROM customers t WHERE t::text ILIKE '%QA-TEST%'
    UNION ALL SELECT 'product', id, organization_id, name, NULL, created_at FROM products t WHERE t::text ILIKE '%QA-TEST%'
),
linked AS (
    SELECT 'purchase' AS kind, id, organization_id, coalesce(purchase_number, invoice_number) AS label, status::text, created_at FROM purchase_invoices t
     WHERE t::text ILIKE '%QA-TEST%' OR supplier_id IN (SELECT id FROM named WHERE kind = 'supplier')
        OR id IN (SELECT invoice_id FROM purchase_invoice_items WHERE product_id IN (SELECT id FROM named WHERE kind = 'product'))
),
qa_rows AS (
SELECT * FROM named
UNION ALL SELECT * FROM linked
UNION ALL SELECT 'grn', id, organization_id, grn_number, status::text, created_at FROM goods_receipt_notes t
     WHERE t::text ILIKE '%QA-TEST%' OR purchase_id IN (SELECT id FROM linked) OR supplier_id IN (SELECT id FROM named WHERE kind = 'supplier')
UNION ALL SELECT 'purchase_return', id, organization_id, return_number, status::text, created_at FROM purchase_returns t
     WHERE t::text ILIKE '%QA-TEST%' OR purchase_id IN (SELECT id FROM linked) OR supplier_id IN (SELECT id FROM named WHERE kind = 'supplier')
UNION ALL SELECT 'supplier_invoice', id, organization_id, supplier_invoice_number, status::text, created_at FROM supplier_invoices t
     WHERE t::text ILIKE '%QA-TEST%' OR purchase_id IN (SELECT id FROM linked) OR supplier_id IN (SELECT id FROM named WHERE kind = 'supplier')
UNION ALL SELECT 'supplier_payment', id, organization_id, payment_number, NULL, created_at FROM supplier_payments t
     WHERE t::text ILIKE '%QA-TEST%' OR supplier_id IN (SELECT id FROM named WHERE kind = 'supplier')
UNION ALL SELECT 'expense', id, organization_id, expense_number, status::text, created_at FROM expenses t WHERE t::text ILIKE '%QA-TEST%'
UNION ALL SELECT 'quotation', id, organization_id, quotation_number, status::text, created_at FROM quotations t
     WHERE t::text ILIKE '%QA-TEST%' OR customer_id IN (SELECT id FROM named WHERE kind = 'customer')
)
SELECT kind, label, status, id, organization_id, created_at FROM qa_rows ORDER BY organization_id, kind, created_at;

-- B2. Stock ledger entries they created: every movement of a QA-TEST product, plus any
-- movement written by a QA GRN / return on a real product (matched by the note,
-- "GRN <number> ..." / "Return <number> ...").
WITH named AS (
    SELECT 'supplier' AS kind, id, organization_id, name AS label, NULL::text AS status, created_at FROM suppliers t WHERE t::text ILIKE '%QA-TEST%'
    UNION ALL SELECT 'customer', id, organization_id, name, NULL, created_at FROM customers t WHERE t::text ILIKE '%QA-TEST%'
    UNION ALL SELECT 'product', id, organization_id, name, NULL, created_at FROM products t WHERE t::text ILIKE '%QA-TEST%'
),
linked AS (
    SELECT 'purchase' AS kind, id, organization_id, coalesce(purchase_number, invoice_number) AS label, status::text, created_at FROM purchase_invoices t
     WHERE t::text ILIKE '%QA-TEST%' OR supplier_id IN (SELECT id FROM named WHERE kind = 'supplier')
        OR id IN (SELECT invoice_id FROM purchase_invoice_items WHERE product_id IN (SELECT id FROM named WHERE kind = 'product'))
),
qa_rows AS (
SELECT * FROM named
UNION ALL SELECT * FROM linked
UNION ALL SELECT 'grn', id, organization_id, grn_number, status::text, created_at FROM goods_receipt_notes t
     WHERE t::text ILIKE '%QA-TEST%' OR purchase_id IN (SELECT id FROM linked) OR supplier_id IN (SELECT id FROM named WHERE kind = 'supplier')
UNION ALL SELECT 'purchase_return', id, organization_id, return_number, status::text, created_at FROM purchase_returns t
     WHERE t::text ILIKE '%QA-TEST%' OR purchase_id IN (SELECT id FROM linked) OR supplier_id IN (SELECT id FROM named WHERE kind = 'supplier')
UNION ALL SELECT 'supplier_invoice', id, organization_id, supplier_invoice_number, status::text, created_at FROM supplier_invoices t
     WHERE t::text ILIKE '%QA-TEST%' OR purchase_id IN (SELECT id FROM linked) OR supplier_id IN (SELECT id FROM named WHERE kind = 'supplier')
UNION ALL SELECT 'supplier_payment', id, organization_id, payment_number, NULL, created_at FROM supplier_payments t
     WHERE t::text ILIKE '%QA-TEST%' OR supplier_id IN (SELECT id FROM named WHERE kind = 'supplier')
UNION ALL SELECT 'expense', id, organization_id, expense_number, status::text, created_at FROM expenses t WHERE t::text ILIKE '%QA-TEST%'
UNION ALL SELECT 'quotation', id, organization_id, quotation_number, status::text, created_at FROM quotations t
     WHERE t::text ILIKE '%QA-TEST%' OR customer_id IN (SELECT id FROM named WHERE kind = 'customer')
)
SELECT m.created_at, m.movement_type, m.quantity, m.balance_after, m.note,
       p.name AS product, w.name AS warehouse, m.id, m.organization_id
FROM stock_movements m
LEFT JOIN products p ON p.id = m.product_id
LEFT JOIN warehouses w ON w.id = m.warehouse_id
WHERE m.product_id IN (SELECT id FROM qa_rows WHERE kind = 'product')
   OR EXISTS (SELECT 1 FROM qa_rows q WHERE q.kind = 'grn' AND m.note LIKE 'GRN ' || q.label || ' %')
   OR EXISTS (SELECT 1 FROM qa_rows q WHERE q.kind = 'purchase_return' AND m.note LIKE 'Return ' || q.label || ' %')
ORDER BY m.created_at;

-- B3. Current stock held by QA-TEST products (what a cleanup would have to take out).
WITH named AS (
    SELECT 'supplier' AS kind, id, organization_id, name AS label, NULL::text AS status, created_at FROM suppliers t WHERE t::text ILIKE '%QA-TEST%'
    UNION ALL SELECT 'customer', id, organization_id, name, NULL, created_at FROM customers t WHERE t::text ILIKE '%QA-TEST%'
    UNION ALL SELECT 'product', id, organization_id, name, NULL, created_at FROM products t WHERE t::text ILIKE '%QA-TEST%'
),
linked AS (
    SELECT 'purchase' AS kind, id, organization_id, coalesce(purchase_number, invoice_number) AS label, status::text, created_at FROM purchase_invoices t
     WHERE t::text ILIKE '%QA-TEST%' OR supplier_id IN (SELECT id FROM named WHERE kind = 'supplier')
        OR id IN (SELECT invoice_id FROM purchase_invoice_items WHERE product_id IN (SELECT id FROM named WHERE kind = 'product'))
),
qa_rows AS (
SELECT * FROM named
UNION ALL SELECT * FROM linked
UNION ALL SELECT 'grn', id, organization_id, grn_number, status::text, created_at FROM goods_receipt_notes t
     WHERE t::text ILIKE '%QA-TEST%' OR purchase_id IN (SELECT id FROM linked) OR supplier_id IN (SELECT id FROM named WHERE kind = 'supplier')
UNION ALL SELECT 'purchase_return', id, organization_id, return_number, status::text, created_at FROM purchase_returns t
     WHERE t::text ILIKE '%QA-TEST%' OR purchase_id IN (SELECT id FROM linked) OR supplier_id IN (SELECT id FROM named WHERE kind = 'supplier')
UNION ALL SELECT 'supplier_invoice', id, organization_id, supplier_invoice_number, status::text, created_at FROM supplier_invoices t
     WHERE t::text ILIKE '%QA-TEST%' OR purchase_id IN (SELECT id FROM linked) OR supplier_id IN (SELECT id FROM named WHERE kind = 'supplier')
UNION ALL SELECT 'supplier_payment', id, organization_id, payment_number, NULL, created_at FROM supplier_payments t
     WHERE t::text ILIKE '%QA-TEST%' OR supplier_id IN (SELECT id FROM named WHERE kind = 'supplier')
UNION ALL SELECT 'expense', id, organization_id, expense_number, status::text, created_at FROM expenses t WHERE t::text ILIKE '%QA-TEST%'
UNION ALL SELECT 'quotation', id, organization_id, quotation_number, status::text, created_at FROM quotations t
     WHERE t::text ILIKE '%QA-TEST%' OR customer_id IN (SELECT id FROM named WHERE kind = 'customer')
)
SELECT p.name AS product, w.name AS warehouse, ws.on_hand_quantity, ws.id
FROM warehouse_stock ws
JOIN products p ON p.id = ws.product_id
LEFT JOIN warehouses w ON w.id = ws.warehouse_id
WHERE ws.product_id IN (SELECT id FROM qa_rows WHERE kind = 'product');

-- B4. Accounting journal entries posted for any QA row.
WITH named AS (
    SELECT 'supplier' AS kind, id, organization_id, name AS label, NULL::text AS status, created_at FROM suppliers t WHERE t::text ILIKE '%QA-TEST%'
    UNION ALL SELECT 'customer', id, organization_id, name, NULL, created_at FROM customers t WHERE t::text ILIKE '%QA-TEST%'
    UNION ALL SELECT 'product', id, organization_id, name, NULL, created_at FROM products t WHERE t::text ILIKE '%QA-TEST%'
),
linked AS (
    SELECT 'purchase' AS kind, id, organization_id, coalesce(purchase_number, invoice_number) AS label, status::text, created_at FROM purchase_invoices t
     WHERE t::text ILIKE '%QA-TEST%' OR supplier_id IN (SELECT id FROM named WHERE kind = 'supplier')
        OR id IN (SELECT invoice_id FROM purchase_invoice_items WHERE product_id IN (SELECT id FROM named WHERE kind = 'product'))
),
qa_rows AS (
SELECT * FROM named
UNION ALL SELECT * FROM linked
UNION ALL SELECT 'grn', id, organization_id, grn_number, status::text, created_at FROM goods_receipt_notes t
     WHERE t::text ILIKE '%QA-TEST%' OR purchase_id IN (SELECT id FROM linked) OR supplier_id IN (SELECT id FROM named WHERE kind = 'supplier')
UNION ALL SELECT 'purchase_return', id, organization_id, return_number, status::text, created_at FROM purchase_returns t
     WHERE t::text ILIKE '%QA-TEST%' OR purchase_id IN (SELECT id FROM linked) OR supplier_id IN (SELECT id FROM named WHERE kind = 'supplier')
UNION ALL SELECT 'supplier_invoice', id, organization_id, supplier_invoice_number, status::text, created_at FROM supplier_invoices t
     WHERE t::text ILIKE '%QA-TEST%' OR purchase_id IN (SELECT id FROM linked) OR supplier_id IN (SELECT id FROM named WHERE kind = 'supplier')
UNION ALL SELECT 'supplier_payment', id, organization_id, payment_number, NULL, created_at FROM supplier_payments t
     WHERE t::text ILIKE '%QA-TEST%' OR supplier_id IN (SELECT id FROM named WHERE kind = 'supplier')
UNION ALL SELECT 'expense', id, organization_id, expense_number, status::text, created_at FROM expenses t WHERE t::text ILIKE '%QA-TEST%'
UNION ALL SELECT 'quotation', id, organization_id, quotation_number, status::text, created_at FROM quotations t
     WHERE t::text ILIKE '%QA-TEST%' OR customer_id IN (SELECT id FROM named WHERE kind = 'customer')
)
SELECT je.entry_number, je.entry_type, je.source_type, je.effective_date, je.status,
       sum(jl.debit) AS debit, sum(jl.credit) AS credit, je.id
FROM journal_entries je
LEFT JOIN journal_lines jl ON jl.journal_entry_id = je.id
WHERE je.source_id IN (SELECT id FROM qa_rows)
GROUP BY je.id
ORDER BY je.effective_date;

-- ---------------------------------------------------------------------------------------
-- C. FK proof: every foreign key pointing at the tables the cleanup touches, with its
-- ON DELETE rule, as it exists in THIS database (production may differ from a fresh one).
-- ---------------------------------------------------------------------------------------
SELECT ct.relname AS child_table, a.attname AS child_column, pt.relname AS parent_table,
       CASE c.confdeltype WHEN 'a' THEN 'NO ACTION' WHEN 'r' THEN 'RESTRICT' WHEN 'c' THEN 'CASCADE' WHEN 'n' THEN 'SET NULL' WHEN 'd' THEN 'SET DEFAULT' END AS on_delete
FROM pg_constraint c
JOIN pg_class ct ON ct.oid = c.conrelid
JOIN pg_class pt ON pt.oid = c.confrelid
JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
WHERE c.contype = 'f'
  AND pt.relname IN ('customers','suppliers','products','purchase_invoices','goods_receipt_notes','purchase_returns',
                     'quotations','supplier_invoices','expenses','journal_entries',
                     'purchase_invoice_items','product_variants','stock_batches','supplier_payments')
ORDER BY pt.relname, ct.relname, a.attname;

ROLLBACK;
