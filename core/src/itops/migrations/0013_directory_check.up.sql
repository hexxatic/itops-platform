-- When a person's account was last checked against AD for their tokens (ADR 0013).
ALTER TABLE itops.users ADD directory_checked_at datetime2(3) NULL;
