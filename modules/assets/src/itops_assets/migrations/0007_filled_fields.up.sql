-- Fields a connector owns that a person (or a merge) filled while the source had no value
-- (ADR 0017): a sync keeps them until the source sends a value of its own.
CREATE TABLE assets.filled_fields (
    record_id uniqueidentifier NOT NULL
        CONSTRAINT fk_filled_fields_record REFERENCES assets.records (id) ON DELETE CASCADE,
    field_key nvarchar(40) NOT NULL,
    CONSTRAINT pk_filled_fields PRIMARY KEY (record_id, field_key)
);
