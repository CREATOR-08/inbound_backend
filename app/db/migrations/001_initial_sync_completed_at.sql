ALTER TABLE user_accounts
ADD COLUMN IF NOT EXISTS initial_sync_completed_at TIMESTAMPTZ;
