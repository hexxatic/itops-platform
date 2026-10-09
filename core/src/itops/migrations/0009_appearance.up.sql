-- Each person's light/dark choice (auto = follow the system), from /account.
ALTER TABLE itops.users ADD appearance nvarchar(8) NOT NULL
    CONSTRAINT df_users_appearance DEFAULT N'auto';
