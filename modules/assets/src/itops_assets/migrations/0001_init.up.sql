-- Assets (ADR 0009). Every record has a type (core record types) and a name; its other
-- values are the type's fields. People edit directly (audited); AI changes are proposals.

CREATE TABLE assets.records (
    id uniqueidentifier NOT NULL CONSTRAINT pk_records PRIMARY KEY
        CONSTRAINT df_records_id DEFAULT newsequentialid(),
    type_key nvarchar(60) NOT NULL,
    name nvarchar(300) NOT NULL,
    fields nvarchar(max) NULL
        CONSTRAINT ck_records_fields_json CHECK (fields IS NULL OR ISJSON(fields) = 1),
    created_by nvarchar(200) NOT NULL,
    created_at datetime2 NOT NULL CONSTRAINT df_records_created DEFAULT sysutcdatetime(),
    updated_by nvarchar(200) NOT NULL,
    updated_at datetime2 NOT NULL CONSTRAINT df_records_updated DEFAULT sysutcdatetime()
);
GO
CREATE INDEX ix_records_type_name ON assets.records (type_key, name);
GO

-- One row per field value, rebuilt on every change: queryable ("certificates expiring in
-- 30 days", "devices at the north plant", "IPs on switch X").
CREATE TABLE assets.record_fields (
    record_id uniqueidentifier NOT NULL CONSTRAINT fk_record_fields_record
        REFERENCES assets.records (id) ON DELETE CASCADE,
    field_key nvarchar(40) NOT NULL,
    position int NOT NULL,
    kind nvarchar(20) NOT NULL,
    value_text nvarchar(450) NULL,
    value_date date NULL,
    value_number float NULL,
    CONSTRAINT pk_record_fields PRIMARY KEY (record_id, field_key, position)
);
GO
CREATE INDEX ix_record_fields_date ON assets.record_fields (field_key, value_date)
    WHERE value_date IS NOT NULL;
GO
CREATE INDEX ix_record_fields_text ON assets.record_fields (field_key, value_text);
GO

-- AI-drafted changes, approved or rejected by a person (assets.publish).
CREATE TABLE assets.proposals (
    id int IDENTITY(1, 1) NOT NULL CONSTRAINT pk_proposals PRIMARY KEY,
    record_id uniqueidentifier NULL,        -- NULL = a new record
    type_key nvarchar(60) NOT NULL,
    name nvarchar(300) NOT NULL,
    fields nvarchar(max) NULL
        CONSTRAINT ck_proposals_fields_json CHECK (fields IS NULL OR ISJSON(fields) = 1),
    note nvarchar(500) NOT NULL CONSTRAINT df_proposals_note DEFAULT N'',
    status nvarchar(20) NOT NULL CONSTRAINT df_proposals_status DEFAULT N'pending'
        CONSTRAINT ck_proposals_status CHECK (status IN (N'pending', N'approved', N'rejected')),
    proposed_by nvarchar(200) NOT NULL,
    proposed_via_ai bit NOT NULL,
    created_at datetime2 NOT NULL CONSTRAINT df_proposals_created DEFAULT sysutcdatetime(),
    decided_by nvarchar(200) NULL,
    decided_at datetime2 NULL
);
GO
CREATE INDEX ix_proposals_status ON assets.proposals (status, created_at);
