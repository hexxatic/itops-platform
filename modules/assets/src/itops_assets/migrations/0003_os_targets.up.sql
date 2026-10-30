-- OS readiness (ADR 0019): the operating systems devices are checked against, with their
-- hardware requirements, kept by admins. Windows 11 is the first; the next Windows is a new
-- row, not new code.
CREATE TABLE assets.os_targets (
    id int IDENTITY(1, 1) NOT NULL CONSTRAINT pk_os_targets PRIMARY KEY,
    name nvarchar(100) NOT NULL CONSTRAINT uq_os_targets_name UNIQUE,
    upgrades_from nvarchar(max) NOT NULL
        CONSTRAINT ck_os_targets_from_json CHECK (ISJSON(upgrades_from) = 1),
    processors nvarchar(max) NOT NULL CONSTRAINT df_os_targets_processors DEFAULT N'',
    min_memory_gb float NULL,
    min_storage_gb float NULL,
    min_tpm nvarchar(10) NULL,
    source nvarchar(500) NOT NULL CONSTRAINT df_os_targets_source DEFAULT N'',
    notes nvarchar(2000) NOT NULL CONSTRAINT df_os_targets_notes DEFAULT N'',
    updated_by nvarchar(200) NOT NULL,
    updated_at datetime2 NOT NULL CONSTRAINT df_os_targets_updated DEFAULT sysutcdatetime()
);
