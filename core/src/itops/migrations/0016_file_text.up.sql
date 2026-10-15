-- Text of stored files, page by page (ADR 0015), read once per file content: copies of a
-- file (same sha256) share it. Search and AI suggestions use it.
CREATE TABLE itops.file_text (
    sha256 char(64) NOT NULL CONSTRAINT pk_file_text PRIMARY KEY,
    pages nvarchar(max) NOT NULL              -- JSON ["page 1 text", ...]; [] = none found
        CONSTRAINT ck_file_text_pages_json CHECK (ISJSON(pages) = 1),
    reader_version int NOT NULL,              -- re-read when the readers improve
    extracted_at datetime2(3) NOT NULL CONSTRAINT df_file_text_extracted DEFAULT sysutcdatetime()
);
GO
-- What each module's search entries were last built with: raising a module's
-- module.search.version re-indexes it once at startup (e.g. Docs, when attachment contents
-- became searchable).
CREATE TABLE itops.search_versions (
    module nvarchar(64) NOT NULL CONSTRAINT pk_search_versions PRIMARY KEY,
    version int NOT NULL,
    rebuilt_at datetime2(3) NOT NULL CONSTRAINT df_search_versions_rebuilt DEFAULT sysutcdatetime()
);
