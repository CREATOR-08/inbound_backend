ALTER TABLE user_accounts
ADD COLUMN IF NOT EXISTS incremental_sync_status VARCHAR(16);
