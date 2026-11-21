-- READ ONLY. Are product rows carrying inline base64 (data: URIs) or other large text?
-- A product row is pulled into every order / invoice / quotation / delivery line query,
-- so its size is paid on each of them. Changes nothing.
BEGIN TRANSACTION READ ONLY;

-- 1. Inline base64 per column (count of products, total bytes).
SELECT 'cover_image' AS col, count(*) AS rows_with_data_uri, sum(length(cover_image)) AS bytes
  FROM products WHERE cover_image ILIKE 'data:%'
UNION ALL SELECT 'images', count(*), sum(length(images::text))
  FROM products WHERE images::text ILIKE '%data:%'
UNION ALL SELECT 'other_attachments', count(*), sum(length(other_attachments::text))
  FROM products WHERE other_attachments::text ILIKE '%data:%'
UNION ALL SELECT 'file slots', count(*), sum(coalesce(length(product_manual), 0) + coalesce(length(product_datasheet), 0)
                 + coalesce(length(product_catalog_brochure), 0) + coalesce(length(compliance_certificate), 0)
                 + coalesce(length(warranty_document), 0) + coalesce(length(download_file), 0)
                 + coalesce(length(product_video), 0))
  FROM products
 WHERE product_manual ILIKE 'data:%' OR product_datasheet ILIKE 'data:%' OR product_catalog_brochure ILIKE 'data:%'
    OR compliance_certificate ILIKE 'data:%' OR warranty_document ILIKE 'data:%' OR download_file ILIKE 'data:%'
    OR product_video ILIKE 'data:%'
UNION ALL SELECT 'product_variants.image_url', count(*), sum(length(image_url))
  FROM product_variants WHERE image_url ILIKE 'data:%';

-- 2. Average and largest product row (bytes as stored), and the 15 biggest products.
SELECT count(*) AS products,
       round(avg(pg_column_size(p.*)))  AS avg_row_bytes,
       max(pg_column_size(p.*))         AS max_row_bytes
  FROM products p;

SELECT p.product_id, p.name, pg_column_size(p.*) AS row_bytes,
       coalesce(length(p.cover_image), 0)   AS cover_image_len,
       coalesce(length(p.images::text), 0)  AS images_len,
       coalesce(length(p.description), 0)   AS description_len
  FROM products p
 ORDER BY pg_column_size(p.*) DESC
 LIMIT 15;

ROLLBACK;
