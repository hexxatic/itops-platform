-- A proposal can be to merge its record into another one (two records that are one thing).
ALTER TABLE assets.proposals ADD merge_into uniqueidentifier NULL;
