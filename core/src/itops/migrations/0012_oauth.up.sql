-- OAuth for MCP clients that can't use personal access tokens, like ChatGPT (ADR 0013).
-- Codes and tokens are stored as SHA-256 hashes only, like personal access tokens.
CREATE TABLE itops.oauth_clients (
    client_id nvarchar(64) NOT NULL CONSTRAINT pk_oauth_clients PRIMARY KEY,
    info nvarchar(max) NOT NULL,                 -- the registered client metadata (JSON)
    created_at datetime2(3) NOT NULL CONSTRAINT df_oauth_clients_created DEFAULT sysutcdatetime()
);
GO
-- A browser on its way through sign-in and consent.
CREATE TABLE itops.oauth_requests (
    id_hash char(64) NOT NULL CONSTRAINT pk_oauth_requests PRIMARY KEY,
    client_id nvarchar(64) NOT NULL,
    params nvarchar(max) NOT NULL,               -- the authorization request (JSON)
    expires_at datetime2(3) NOT NULL
);
GO
CREATE TABLE itops.oauth_codes (
    code_hash char(64) NOT NULL CONSTRAINT pk_oauth_codes PRIMARY KEY,
    client_id nvarchar(64) NOT NULL,
    user_id nvarchar(64) NOT NULL CONSTRAINT fk_oauth_codes_user REFERENCES itops.users (id),
    params nvarchar(max) NOT NULL,               -- scopes, PKCE challenge, redirect URI, resource
    expires_at datetime2(3) NOT NULL
);
GO
CREATE TABLE itops.oauth_tokens (
    id int IDENTITY(1,1) NOT NULL CONSTRAINT pk_oauth_tokens PRIMARY KEY,
    grant_id uniqueidentifier NOT NULL,          -- one consent; refreshes stay in it
    token_hash char(64) NOT NULL CONSTRAINT uq_oauth_tokens_hash UNIQUE,
    kind nvarchar(10) NOT NULL,                  -- access, refresh
    client_id nvarchar(64) NOT NULL,
    user_id nvarchar(64) NOT NULL CONSTRAINT fk_oauth_tokens_user REFERENCES itops.users (id),
    scopes nvarchar(200) NOT NULL,
    resource nvarchar(500) NULL,
    created_at datetime2(3) NOT NULL CONSTRAINT df_oauth_tokens_created DEFAULT sysutcdatetime(),
    expires_at datetime2(3) NOT NULL,
    last_used_at datetime2(3) NULL,
    revoked_at datetime2(3) NULL
);
GO
CREATE INDEX ix_oauth_tokens_user ON itops.oauth_tokens (user_id, grant_id);
