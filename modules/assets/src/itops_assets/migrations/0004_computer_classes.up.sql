-- Computer classes: the model patterns that make a computer Engineering or Standard, kept by
-- admins. One row per tier; a missing row means the tier's preset.
CREATE TABLE assets.computer_classes (
    tier nvarchar(20) NOT NULL CONSTRAINT pk_computer_classes PRIMARY KEY,
    models nvarchar(max) NOT NULL CONSTRAINT df_computer_classes_models DEFAULT N'',
    updated_by nvarchar(200) NOT NULL,
    updated_at datetime2 NOT NULL CONSTRAINT df_computer_classes_updated DEFAULT sysutcdatetime()
);
