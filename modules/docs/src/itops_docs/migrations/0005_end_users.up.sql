-- A collection published for end users: its published documents are offered, read-only,
-- to a separate end-user help app through GET /api/docs/enduser (ADR 0022). Only admins
-- (docs.collections.manage, human-only) set it.
ALTER TABLE docs.collections ADD for_end_users bit NOT NULL
    CONSTRAINT df_collections_for_end_users DEFAULT 0;
