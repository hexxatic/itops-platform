CREATE TABLE itops.audit_log (
    id bigint IDENTITY(1,1) NOT NULL CONSTRAINT pk_audit_log PRIMARY KEY,
    at datetime2(3) NOT NULL CONSTRAINT df_audit_log_at DEFAULT sysutcdatetime(),
    module nvarchar(50) NOT NULL,
    action nvarchar(100) NOT NULL,        -- 'document.published'
    entity_kind nvarchar(50) NULL,
    entity_id nvarchar(100) NULL,
    entity_title nvarchar(300) NULL,      -- as it was at the time
    actor_user nvarchar(200) NOT NULL,    -- UPN, or 'system'
    actor_name nvarchar(200) NOT NULL,
    via_ai bit NOT NULL,
    client nvarchar(20) NOT NULL,         -- 'web' | 'api' | 'mcp' | 'system'
    summary nvarchar(400) NOT NULL,
    details nvarchar(max) NULL            -- JSON
);
GO
CREATE INDEX ix_audit_log_entity ON itops.audit_log (module, entity_kind, entity_id, id DESC);
GO
CREATE INDEX ix_audit_log_actor ON itops.audit_log (actor_user, id DESC);
GO
-- Append-only: the application can add entries, never change or remove them.
CREATE TRIGGER itops.tr_audit_log_append_only ON itops.audit_log
INSTEAD OF UPDATE, DELETE
AS
BEGIN
    SET NOCOUNT ON;
    THROW 50001, 'itops.audit_log is append-only.', 1;
END;
