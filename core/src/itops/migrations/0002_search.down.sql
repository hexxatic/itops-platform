IF EXISTS (SELECT 1 FROM sys.fulltext_indexes WHERE object_id = OBJECT_ID(N'itops.search_passages'))
    EXEC(N'DROP FULLTEXT INDEX ON itops.search_passages');
GO
IF EXISTS (SELECT 1 FROM sys.fulltext_catalogs WHERE name = N'itops_search')
    EXEC(N'DROP FULLTEXT CATALOG itops_search');
GO
DROP TABLE itops.search_passages;
