-- The audience area each how-to's search entries carry (ADR 0028), so a change of audience
-- re-indexes it.
ALTER TABLE kb.indexed ADD area nvarchar(200) NULL;
