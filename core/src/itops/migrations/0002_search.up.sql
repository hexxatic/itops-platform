-- Core search index (ADR 0003): every module's searchable content as citable passages.
-- Only published content goes in. read_permission filters results per searcher.
CREATE TABLE itops.search_passages (
    id bigint IDENTITY(1, 1) NOT NULL CONSTRAINT pk_search_passages PRIMARY KEY,
    module nvarchar(64) NOT NULL,
    kind nvarchar(64) NOT NULL,            -- e.g. 'document', 'ticket', 'asset'
    entity_id nvarchar(64) NOT NULL,
    title nvarchar(300) NOT NULL,
    url nvarchar(500) NOT NULL,            -- path to the entity, e.g. /docs/d/<slug>
    label nvarchar(300) NOT NULL,          -- where in the entity: 'Failover > Step 3'
    anchor nvarchar(200) NULL,             -- #anchor within the page
    text nvarchar(max) NOT NULL,
    read_permission nvarchar(200) NOT NULL,
    updated_at datetime2 NOT NULL CONSTRAINT df_search_updated DEFAULT sysutcdatetime()
);
GO
CREATE INDEX ix_search_entity ON itops.search_passages (module, kind, entity_id);
GO
-- Full-Text ranking where the feature is installed (dev boxes, production). Without it
-- (the Linux CI container) search falls back to plain matching.
IF FULLTEXTSERVICEPROPERTY('IsFullTextInstalled') = 1
BEGIN
    EXEC(N'CREATE FULLTEXT CATALOG itops_search');
    EXEC(N'CREATE FULLTEXT INDEX ON itops.search_passages (title, label, text LANGUAGE 1033)
          KEY INDEX pk_search_passages ON itops_search WITH CHANGE_TRACKING AUTO');
END
