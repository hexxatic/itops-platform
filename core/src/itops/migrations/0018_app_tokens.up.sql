-- App tokens (ADR 0021): another internal application reads the REST API as itself,
-- with read permissions an admin chose (hash only; the token is shown once).
CREATE TABLE itops.app_tokens (
    id int IDENTITY(1, 1) NOT NULL CONSTRAINT pk_app_tokens PRIMARY KEY,
    slug nvarchar(50) NOT NULL,               -- the app's short name: principal app:<slug>
    name nvarchar(100) NOT NULL,
    token_hash char(64) NOT NULL CONSTRAINT uq_app_tokens_hash UNIQUE,
    prefix nvarchar(20) NOT NULL,             -- first characters, to recognise a token
    permissions nvarchar(2000) NOT NULL,      -- comma-separated *.read permissions
    created_by nvarchar(200) NOT NULL,
    created_at datetime2 NOT NULL CONSTRAINT df_app_tokens_created DEFAULT sysutcdatetime(),
    expires_at datetime2 NOT NULL,
    last_used_at datetime2 NULL,
    revoked_at datetime2 NULL
);
GO
CREATE INDEX ix_app_tokens_slug ON itops.app_tokens (slug);
