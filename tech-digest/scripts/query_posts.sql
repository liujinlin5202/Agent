SELECT id, user_id, token, is_used, created_at
FROM refresh_tokens WHERE user_id = 0 /* ← 改成目标账号 ID */ ORDER BY id DESC LIMIT 6;
