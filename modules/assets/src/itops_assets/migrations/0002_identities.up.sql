-- What identifies a device across sources (ADR 0017): normalised serial numbers, MAC
-- addresses and short host names, rebuilt with a record's other queryable values. Synced
-- records from a new source are matched against these before a new record is created.

CREATE TABLE assets.identities (
    record_id uniqueidentifier NOT NULL CONSTRAINT fk_identities_record
        REFERENCES assets.records (id) ON DELETE CASCADE,
    kind nvarchar(10) NOT NULL
        CONSTRAINT ck_identities_kind CHECK (kind IN (N'serial', N'mac', N'host')),
    value nvarchar(200) NOT NULL,
    CONSTRAINT pk_identities PRIMARY KEY (record_id, kind, value)
);
GO
CREATE INDEX ix_identities_value ON assets.identities (kind, value);
