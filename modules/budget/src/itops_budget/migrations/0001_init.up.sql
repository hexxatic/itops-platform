-- Budget (ADR 0016). The forecast itself is worked out on every view from the fleet (Assets),
-- HR and purchase history (Reports datasets); this schema holds only Budget's own inputs
-- (assumptions, the price list) and the versions people save.
CREATE TABLE budget.settings (
    [key] nvarchar(60) NOT NULL CONSTRAINT pk_settings PRIMARY KEY,
    value nvarchar(max) NOT NULL CONSTRAINT ck_settings_value_json CHECK (ISJSON(value) = 1),
    updated_by nvarchar(200) NOT NULL,
    updated_at datetime2(3) NOT NULL CONSTRAINT df_settings_updated DEFAULT sysutcdatetime()
);
GO
CREATE TABLE budget.prices (
    id int IDENTITY(1, 1) NOT NULL CONSTRAINT pk_prices PRIMARY KEY,
    item nvarchar(200) NOT NULL,
    category nvarchar(20) NOT NULL
        CONSTRAINT ck_prices_category CHECK (category IN ('laptop', 'desktop', 'kit')),
    unit_price decimal(12, 2) NOT NULL CONSTRAINT ck_prices_unit_price CHECK (unit_price >= 0),
    source nvarchar(300) NOT NULL CONSTRAINT df_prices_source DEFAULT '',
    quoted_on date NULL,
    updated_by nvarchar(200) NOT NULL,
    updated_at datetime2(3) NOT NULL CONSTRAINT df_prices_updated DEFAULT sysutcdatetime()
);
GO
-- A saved version freezes the inputs and the forecast (JSON), so it can be explained later.
CREATE TABLE budget.versions (
    id int IDENTITY(1, 1) NOT NULL CONSTRAINT pk_versions PRIMARY KEY,
    title nvarchar(200) NOT NULL,
    status nvarchar(20) NOT NULL
        CONSTRAINT ck_versions_status CHECK (status IN ('saved', 'adopted')),
    total decimal(14, 2) NULL,
    snapshot nvarchar(max) NOT NULL CONSTRAINT ck_versions_snapshot_json CHECK (ISJSON(snapshot) = 1),
    created_by nvarchar(200) NOT NULL,
    created_at datetime2(3) NOT NULL CONSTRAINT df_versions_created DEFAULT sysutcdatetime(),
    adopted_by nvarchar(200) NULL,
    adopted_at datetime2(3) NULL
);
