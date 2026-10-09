-- People who have signed in (Entra), with the roles from their groups at last sign-in,
-- and their personal access tokens for AI tools and scripts (hash only).
CREATE TABLE itops.users (
    id nvarchar(64) NOT NULL CONSTRAINT pk_users PRIMARY KEY,   -- Entra object id (oid)
    upn nvarchar(256) NOT NULL,
    display_name nvarchar(256) NOT NULL,
    roles nvarchar(200) NOT NULL CONSTRAINT df_users_roles DEFAULT N'',
    first_seen datetime2 NOT NULL CONSTRAINT df_users_first_seen DEFAULT sysutcdatetime(),
    last_sign_in datetime2 NOT NULL CONSTRAINT df_users_last_sign_in DEFAULT sysutcdatetime()
);
GO
CREATE TABLE itops.api_tokens (
    id int IDENTITY(1, 1) NOT NULL CONSTRAINT pk_api_tokens PRIMARY KEY,
    user_id nvarchar(64) NOT NULL CONSTRAINT fk_api_tokens_user REFERENCES itops.users (id),
    name nvarchar(100) NOT NULL,
    token_hash char(64) NOT NULL CONSTRAINT uq_api_tokens_hash UNIQUE,
    prefix nvarchar(20) NOT NULL,             -- first characters, to recognise a token
    created_at datetime2 NOT NULL CONSTRAINT df_api_tokens_created DEFAULT sysutcdatetime(),
    expires_at datetime2 NOT NULL,
    last_used_at datetime2 NULL,
    revoked_at datetime2 NULL
);
GO
CREATE INDEX ix_api_tokens_user ON itops.api_tokens (user_id);
