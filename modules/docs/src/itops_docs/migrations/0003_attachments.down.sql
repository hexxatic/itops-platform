ALTER TABLE docs.versions DROP CONSTRAINT ck_versions_attachments_json;
GO
ALTER TABLE docs.versions DROP COLUMN attachments;
