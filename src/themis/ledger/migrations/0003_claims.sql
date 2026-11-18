-- Migration 0003: claim provenance (docs/m1-ingest-extract.md §2.5).
-- Applies to derived nodes with rule 'table_result' (the only claim rule in M1).
-- V24 (numbers re-parse from blob bytes) needs blob contents and lives in the DAO.
-- Note: json_type() of a missing path is NULL, and NULL comparisons never raise, so every
-- type check below is written with IFNULL / IS NOT.

CREATE TRIGGER nodes_validate_claim BEFORE INSERT ON nodes
WHEN NEW.kind = 'derived'
 AND json_extract(NEW.provenance_json, '$.rule') = 'table_result'
BEGIN
  -- V21: every `from` / `*_from` reference in the claim is one of its parents.
  SELECT RAISE(ABORT, 'V21: every from in a claim must be one of its parents')
   WHERE EXISTS (
     SELECT 1 FROM json_tree(NEW.content_json) t
      WHERE (t.key = 'from' OR t.key LIKE '%\_from' ESCAPE '\')
        AND (t.type IS NOT 'text'
             OR NOT EXISTS (SELECT 1 FROM json_each(NEW.provenance_json, '$.parents') p
                             WHERE p.value = t.atom)));

  -- V22: the value comes from a cited table cell.
  SELECT RAISE(ABORT, 'V22: a claim value must come from a cited table cell')
   WHERE json_type(NEW.content_json, '$.value.from') IS NOT 'text'
      OR NOT EXISTS (
        SELECT 1 FROM nodes n
         WHERE n.id = json_extract(NEW.content_json, '$.value.from')
           AND n.kind = 'cited'
           AND json_extract(n.provenance_json, '$.locator.type') = 'table_cell');

  -- V23: the value (and pm) equal that cell's parsed number.
  SELECT RAISE(ABORT, 'V23: claim value must equal its cell''s parsed number')
    FROM nodes n
   WHERE n.id = json_extract(NEW.content_json, '$.value.from')
     AND (IFNULL(json_type(NEW.content_json, '$.value.number'), '') NOT IN ('integer', 'real')
          OR IFNULL(json_type(n.content_json, '$.number.value'), '') NOT IN ('integer', 'real')
          OR json_extract(NEW.content_json, '$.value.number')
             != json_extract(n.content_json, '$.number.value')
          OR json_extract(NEW.content_json, '$.value.pm')
             IS NOT json_extract(n.content_json, '$.number.pm'));
END;
