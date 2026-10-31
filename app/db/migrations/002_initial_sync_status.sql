ALTER TABLE user_accounts
ADD COLUMN IF NOT EXISTS initial_sync_status VARCHAR(16);
