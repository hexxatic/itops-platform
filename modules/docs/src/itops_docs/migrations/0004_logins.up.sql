-- Logins kept in 1Password belong to a version, like attachments: adding or changing one
-- makes a draft; readers see the published version's list. Never the secret itself.
-- JSON: [{"label", "ref"}], ref an op:// reference or a 1Password item link.
ALTER TABLE docs.versions ADD logins nvarchar(max) NULL
    CONSTRAINT ck_versions_logins_json CHECK (logins IS NULL OR ISJSON(logins) = 1);
