-- READ ONLY. Which warehouse each firm's opening stock (and any "no warehouse given"
-- stock) would land in, and firms where that choice is unclear. Changes nothing.
BEGIN TRANSACTION READ ONLY;

-- 1. Per firm: how many warehouses, how many flagged default, and whether a default is inactive.
--    Healthy = default_count 1 and default_inactive false.
SELECT o.name                                              AS organization,
       count(w.id)                                         AS warehouses,
       count(w.id) FILTER (WHERE w.is_default)             AS default_count,
       bool_or(w.is_default AND NOT w.is_active)           AS default_inactive,
       string_agg(w.code || ' ' || w.name, ', ' ORDER BY w.created_at)
           FILTER (WHERE w.is_default)                     AS default_warehouses
FROM organizations o
LEFT JOIN warehouses w ON w.organization_id = o.id
GROUP BY o.id, o.name
HAVING count(w.id) FILTER (WHERE w.is_default) <> 1
    OR bool_or(w.is_default AND NOT w.is_active)
ORDER BY o.name;

-- 2. Firms with warehouses but none flagged default: the code would silently promote the
--    OLDEST one (by created_at). This shows which one that would be.
SELECT DISTINCT ON (w.organization_id)
       o.name AS organization, w.code, w.name, w.is_active, w.created_at
FROM warehouses w
JOIN organizations o ON o.id = w.organization_id
WHERE NOT EXISTS (SELECT 1 FROM warehouses d
                  WHERE d.organization_id = w.organization_id AND d.is_default)
ORDER BY w.organization_id, w.created_at;

ROLLBACK;
