ALTER TABLE docs.documents DROP CONSTRAINT fk_documents_current_version;
GO
DROP TABLE docs.document_tags;
GO
DROP TABLE docs.versions;
GO
DROP TABLE docs.documents;
GO
DROP TABLE docs.collections;
