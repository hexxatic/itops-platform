-- Attachments (PDFs, Office files, Visio, ...) belong to a version, like its content and
-- fields: adding or removing one makes a draft; readers see the published version's list.
-- JSON: [{"id", "filename", "size", "mime"}]; the bytes live in the core file store.
ALTER TABLE docs.versions ADD attachments nvarchar(max) NULL
    CONSTRAINT ck_versions_attachments_json CHECK (attachments IS NULL OR ISJSON(attachments) = 1);
