-- Visibility of areas by AD group (ADR 0012): which areas are restricted, people's groups,
-- and which area each search passage belongs to.
CREATE TABLE itops.area_restrictions (
    module nvarchar(50) NOT NULL,
    kind nvarchar(50) NOT NULL,                    -- the module's area kind: 'collection', 'type'
    area_key nvarchar(200) NOT NULL,
    group_names nvarchar(2000) NOT NULL CONSTRAINT df_area_restrictions_groups DEFAULT N'',
    no_ai bit NOT NULL CONSTRAINT df_area_restrictions_no_ai DEFAULT 0,
    updated_by nvarchar(200) NOT NULL,
    updated_at datetime2(3) NOT NULL CONSTRAINT df_area_restrictions_at DEFAULT sysutcdatetime(),
    CONSTRAINT pk_area_restrictions PRIMARY KEY (module, kind, area_key)
);
GO
-- AD groups (sAMAccountNames, lower case, comma-separated) as of sign-in or the token check.
ALTER TABLE itops.users ADD group_names nvarchar(max) NULL;
GO
ALTER TABLE itops.search_passages ADD area nvarchar(300) NULL;  -- '<module>:<kind>:<key>'
