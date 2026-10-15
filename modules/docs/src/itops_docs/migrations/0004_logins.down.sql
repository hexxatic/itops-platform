ALTER TABLE docs.versions DROP CONSTRAINT ck_versions_logins_json;
GO
ALTER TABLE docs.versions DROP COLUMN logins;
