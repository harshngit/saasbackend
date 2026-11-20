-- READ ONLY. Products created before opening stock was posted to a warehouse:
-- opening_stock > 0 but no stock at all in any warehouse (no warehouse_stock rows, or all
-- rows at 0). Nothing is fixed here; the list is for review. Changes nothing.
BEGIN TRANSACTION READ ONLY;

SELECT o.name                              AS organization,
       p.product_id,
       p.name,
       p.sku,
       p.opening_stock,
       p.total_inventory                   AS catalog_counter,
       COALESCE(ws.on_hand, 0)             AS warehouse_on_hand,
       p.inventory_tracking,
       p.serial_number_tracking,
       EXISTS (SELECT 1 FROM product_variants v WHERE v.product_id = p.id) AS has_variants,
       p.created_at
FROM products p
JOIN organizations o ON o.id = p.organization_id
LEFT JOIN (
    SELECT product_id, SUM(on_hand_quantity) AS on_hand
    FROM warehouse_stock
    GROUP BY product_id
) ws ON ws.product_id = p.id
WHERE COALESCE(p.opening_stock, 0) > 0
  AND COALESCE(ws.on_hand, 0) = 0
ORDER BY o.name, p.created_at;

ROLLBACK;
