-- The help site shows documents one by one, not whole collections (ADR 0028): a publisher
-- ticks "Show on the help site" on a document and picks who it's for. An audience is an
-- area (ADR 0012) admins restrict to AD groups; no audience means everyone who signs in.
CREATE TABLE docs.audiences (
    id int IDENTITY(1, 1) NOT NULL CONSTRAINT pk_audiences PRIMARY KEY,
    name nvarchar(200) NOT NULL,
    description nvarchar(1000) NOT NULL CONSTRAINT df_audiences_description DEFAULT N'',
    sort_order int NOT NULL CONSTRAINT df_audiences_sort_order DEFAULT 0
);
ALTER TABLE docs.documents ADD
    help_site bit NOT NULL CONSTRAINT df_documents_help_site DEFAULT 0,
    audience_id int NULL CONSTRAINT fk_documents_audience REFERENCES docs.audiences (id);
GO
-- Documents in a collection published for end users (ADR 0022) stay on the help site.
UPDATE d SET help_site = 1
FROM docs.documents d
JOIN docs.collections c ON c.id = d.collection_id
WHERE c.for_end_users = 1;
ALTER TABLE docs.collections DROP CONSTRAINT df_collections_for_end_users;
GO
ALTER TABLE docs.collections DROP COLUMN for_end_users;
