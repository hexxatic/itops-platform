ALTER TABLE docs.collections ADD for_end_users bit NOT NULL
    CONSTRAINT df_collections_for_end_users DEFAULT 0;
GO
UPDATE c SET for_end_users = 1
FROM docs.collections c
WHERE EXISTS (SELECT 1 FROM docs.documents d WHERE d.collection_id = c.id AND d.help_site = 1);
ALTER TABLE docs.documents DROP CONSTRAINT fk_documents_audience;
ALTER TABLE docs.documents DROP CONSTRAINT df_documents_help_site;
GO
ALTER TABLE docs.documents DROP COLUMN help_site, audience_id;
DROP TABLE docs.audiences;
