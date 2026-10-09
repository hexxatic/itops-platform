DROP INDEX ix_documents_type ON docs.documents;
GO
DROP TABLE docs.document_fields;
GO
ALTER TABLE docs.versions DROP CONSTRAINT ck_versions_fields_json;
GO
ALTER TABLE docs.versions DROP COLUMN fields;
GO
ALTER TABLE docs.documents DROP COLUMN type_key;
