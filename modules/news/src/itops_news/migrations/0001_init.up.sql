-- News: one row per post. Drafts are edited in place until published; a published post is
-- changed only by a person with news.publish (corrections), never by AI.
CREATE TABLE news.posts (
    id uniqueidentifier NOT NULL CONSTRAINT pk_posts PRIMARY KEY
        CONSTRAINT df_posts_id DEFAULT newsequentialid(),
    slug nvarchar(200) NOT NULL CONSTRAINT uq_posts_slug UNIQUE,
    title nvarchar(300) NOT NULL,
    category nvarchar(20) NOT NULL
        CONSTRAINT ck_posts_category CHECK (category IN (N'app', N'it')),
    content nvarchar(max) NOT NULL  -- sanitized block JSON (itops.sdk.content)
        CONSTRAINT ck_posts_content_json CHECK (ISJSON(content) = 1),
    status nvarchar(20) NOT NULL CONSTRAINT df_posts_status DEFAULT N'draft'
        CONSTRAINT ck_posts_status CHECK (status IN (N'draft', N'published', N'archived')),
    pinned bit NOT NULL CONSTRAINT df_posts_pinned DEFAULT 0,
    created_by nvarchar(200) NOT NULL,
    created_via_ai bit NOT NULL CONSTRAINT df_posts_via_ai DEFAULT 0,
    created_at datetime2 NOT NULL CONSTRAINT df_posts_created DEFAULT sysutcdatetime(),
    updated_by nvarchar(200) NOT NULL,
    updated_at datetime2 NOT NULL CONSTRAINT df_posts_updated DEFAULT sysutcdatetime(),
    published_by nvarchar(200) NULL,
    published_at datetime2 NULL
);
GO
CREATE INDEX ix_posts_feed ON news.posts (status, pinned DESC, published_at DESC);
