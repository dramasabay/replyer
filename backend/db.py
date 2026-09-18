"""
SQLite database layer. Swap for PostgreSQL later by changing DATABASE_URL
and using an async driver — schema stays the same.
"""
import sqlite3
import os
import time
import json

# DATA_DIR lets deployments (e.g. Docker) point the DB at a mounted volume so
# it survives container rebuilds; defaults to this file's own directory for
# local/dev runs, unchanged from before.
DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(__file__))
DB_PATH = os.path.join(DATA_DIR, "app.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone TEXT UNIQUE NOT NULL,
        telegram_user_id INTEGER,
        display_name TEXT,
        api_id INTEGER,
        encrypted_api_hash BLOB,
        created_at REAL NOT NULL
    )
    """)

    # Migration for DBs created before per-user API credentials existed.
    c.execute("PRAGMA table_info(users)")
    user_cols = {row[1] for row in c.fetchall()}
    if "api_id" not in user_cols:
        c.execute("ALTER TABLE users ADD COLUMN api_id INTEGER")
    if "encrypted_api_hash" not in user_cols:
        c.execute("ALTER TABLE users ADD COLUMN encrypted_api_hash BLOB")
    if "is_approved" not in user_cols:
        c.execute("ALTER TABLE users ADD COLUMN is_approved INTEGER DEFAULT 1")
    if "last_active_at" not in user_cols:
        c.execute("ALTER TABLE users ADD COLUMN last_active_at REAL")

    c.execute("""
    CREATE TABLE IF NOT EXISTS telegram_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        encrypted_session BLOB NOT NULL,
        created_at REAL NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        chat_id INTEGER NOT NULL,
        chat_name TEXT,
        trigger_type TEXT NOT NULL,   -- 'all' | 'sender' | 'keyword'
        trigger_value TEXT,           -- sender_id or keyword, JSON-encodable
        reply_text TEXT,              -- nullable now: reaction-only rules skip this
        reaction_emoji TEXT,          -- e.g. '👍' — nullable, sent via Telegram's native reaction feature
        delay_seconds INTEGER DEFAULT 5,
        active INTEGER DEFAULT 1,
        created_at REAL NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )
    """)

    # Migration for DBs created before reaction_emoji existed.
    c.execute("PRAGMA table_info(rules)")
    existing_cols = {row[1] for row in c.fetchall()}
    if "reaction_emoji" not in existing_cols:
        c.execute("ALTER TABLE rules ADD COLUMN reaction_emoji TEXT")
    if "match_mode" not in existing_cols:
        c.execute("ALTER TABLE rules ADD COLUMN match_mode TEXT DEFAULT 'any'")
    if "cooldown_seconds" not in existing_cols:
        c.execute("ALTER TABLE rules ADD COLUMN cooldown_seconds INTEGER DEFAULT 0")

    # Default / "away" reply — fires for any chat that has no specific rule
    # of its own. One row per user.
    c.execute("""
    CREATE TABLE IF NOT EXISTS default_rules (
        user_id INTEGER PRIMARY KEY,
        reply_text TEXT,
        reaction_emoji TEXT,
        delay_seconds INTEGER DEFAULT 5,
        active INTEGER DEFAULT 0,
        updated_at REAL,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )
    """)

    # Mention reply — triggers when user is mentioned or replied to in groups/channels.
    # One row per user.
    c.execute("""
    CREATE TABLE IF NOT EXISTS mention_rules (
        user_id INTEGER PRIMARY KEY,
        reply_text TEXT,
        reaction_emoji TEXT,
        delay_seconds INTEGER DEFAULT 0,
        cooldown_seconds INTEGER DEFAULT 30,
        active INTEGER DEFAULT 0,
        target_chats TEXT,
        updated_at REAL,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )
    """)

    c.execute("PRAGMA table_info(mention_rules)")
    mention_cols = {row[1] for row in c.fetchall()}
    if "target_chats" not in mention_cols:
        c.execute("ALTER TABLE mention_rules ADD COLUMN target_chats TEXT")

    c.execute("""
    CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rule_id INTEGER NOT NULL,
        message_snippet TEXT,
        replied_at REAL NOT NULL,
        FOREIGN KEY (rule_id) REFERENCES rules(id)
    )
    """)

    conn.commit()
    conn.close()


# ---------- Users ----------

def get_or_create_user(phone: str, telegram_user_id: int, display_name: str, api_id: int = None, encrypted_api_hash: bytes = None):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE phone = ?", (phone,))
    row = c.fetchone()
    if row:
        c.execute(
            "UPDATE users SET telegram_user_id = ?, display_name = ?, api_id = ?, encrypted_api_hash = ? WHERE id = ?",
            (telegram_user_id, display_name, api_id, encrypted_api_hash, row["id"]),
        )
        conn.commit()
        user_id = row["id"]
    else:
        c.execute(
            "INSERT INTO users (phone, telegram_user_id, display_name, api_id, encrypted_api_hash, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (phone, telegram_user_id, display_name, api_id, encrypted_api_hash, time.time()),
        )
        conn.commit()
        user_id = c.lastrowid
    conn.close()
    return user_id


def get_user(user_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


# ---------- Telegram sessions ----------

def save_session(user_id: int, encrypted_session: bytes):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM telegram_sessions WHERE user_id = ?", (user_id,))
    c.execute(
        "INSERT INTO telegram_sessions (user_id, encrypted_session, created_at) VALUES (?, ?, ?)",
        (user_id, encrypted_session, time.time()),
    )
    conn.commit()
    conn.close()


def get_session(user_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM telegram_sessions WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_active_user_ids():
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT DISTINCT user_id FROM rules WHERE active = 1 AND COALESCE((SELECT is_approved FROM users WHERE users.id = rules.user_id), 1) = 1
        UNION
        SELECT user_id FROM default_rules WHERE active = 1 AND COALESCE((SELECT is_approved FROM users WHERE users.id = default_rules.user_id), 1) = 1
        UNION
        SELECT user_id FROM mention_rules WHERE active = 1 AND COALESCE((SELECT is_approved FROM users WHERE users.id = mention_rules.user_id), 1) = 1
    """)
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]


def set_user_approval(user_id: int, is_approved: bool):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE users SET is_approved = ? WHERE id = ?", (1 if is_approved else 0, user_id))
    conn.commit()
    conn.close()


def update_user_active(user_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE users SET last_active_at = ? WHERE id = ?", (time.time(), user_id))
    conn.commit()
    conn.close()


def list_all_users_with_stats():
    """For the admin dashboard: every connected user + their rule/activity counts."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT
            u.id, u.phone, u.display_name, u.created_at,
            COALESCE(u.is_approved, 1) AS is_approved,
            u.last_active_at,
            (SELECT COUNT(*) FROM rules WHERE rules.user_id = u.id AND active = 1) AS active_rules,
            (SELECT COUNT(*) FROM logs
                JOIN rules ON logs.rule_id = rules.id
                WHERE rules.user_id = u.id) AS total_replies
        FROM users u
        ORDER BY u.created_at DESC
    """)
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


# ---------- Rules ----------

def create_rule(user_id, chat_id, chat_name, trigger_type, trigger_value, reply_text, delay_seconds=5, reaction_emoji=None, match_mode="any", cooldown_seconds=0):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        """INSERT INTO rules
           (user_id, chat_id, chat_name, trigger_type, trigger_value, reply_text, reaction_emoji, delay_seconds, active, created_at, match_mode, cooldown_seconds)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
        (user_id, chat_id, chat_name, trigger_type, trigger_value, reply_text, reaction_emoji, delay_seconds, time.time(), match_mode, cooldown_seconds),
    )
    conn.commit()
    rule_id = c.lastrowid
    conn.close()
    return rule_id


def create_batch_rules(user_id, chats, trigger_type, trigger_value, reply_text, delay_seconds=5, reaction_emoji=None, match_mode="any", cooldown_seconds=0):
    """Create rules for multiple chats at once."""
    conn = get_conn()
    c = conn.cursor()
    now = time.time()
    created_ids = []
    for chat in chats:
        cid = chat.get("chat_id")
        cname = chat.get("chat_name") or chat.get("name") or "Chat"
        c.execute(
            """INSERT INTO rules
               (user_id, chat_id, chat_name, trigger_type, trigger_value, reply_text, reaction_emoji, delay_seconds, active, created_at, match_mode, cooldown_seconds)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
            (user_id, cid, cname, trigger_type, trigger_value, reply_text, reaction_emoji, delay_seconds, now, match_mode, cooldown_seconds),
        )
        created_ids.append(c.lastrowid)
    conn.commit()
    conn.close()
    return created_ids


def get_rule(rule_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM rules WHERE id = ?", (rule_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def update_rule(rule_id, user_id=None, reply_text=None, reaction_emoji=None, delay_seconds=None, trigger_type=None, trigger_value=None, match_mode=None, active=None, cooldown_seconds=None):
    conn = get_conn()
    c = conn.cursor()
    fields = []
    values = []
    if reply_text is not None:
        fields.append("reply_text = ?")
        values.append(reply_text)
    if reaction_emoji is not None:
        fields.append("reaction_emoji = ?")
        values.append(reaction_emoji)
    if delay_seconds is not None:
        fields.append("delay_seconds = ?")
        values.append(delay_seconds)
    if trigger_type is not None:
        fields.append("trigger_type = ?")
        values.append(trigger_type)
    if trigger_value is not None:
        fields.append("trigger_value = ?")
        values.append(trigger_value)
    if match_mode is not None:
        fields.append("match_mode = ?")
        values.append(match_mode)
    if cooldown_seconds is not None:
        fields.append("cooldown_seconds = ?")
        values.append(cooldown_seconds)
    if active is not None:
        fields.append("active = ?")
        values.append(1 if active else 0)

    if not fields:
        conn.close()
        return False

    if user_id is not None:
        query = f"UPDATE rules SET {', '.join(fields)} WHERE id = ? AND user_id = ?"
        values.extend([rule_id, user_id])
    else:
        query = f"UPDATE rules SET {', '.join(fields)} WHERE id = ?"
        values.append(rule_id)

    c.execute(query, tuple(values))
    conn.commit()
    success = c.rowcount > 0
    conn.close()
    return success


def list_rules(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM rules WHERE user_id = ? ORDER BY created_at DESC", (user_id,))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def set_rule_active(rule_id, active: bool):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE rules SET active = ? WHERE id = ?", (1 if active else 0, rule_id))
    conn.commit()
    conn.close()


def toggle_all_rules(user_id: int, active: bool):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE rules SET active = ? WHERE user_id = ?", (1 if active else 0, user_id))
    conn.commit()
    count = c.rowcount
    conn.close()
    return count


def delete_rule(rule_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM logs WHERE rule_id = ?", (rule_id,))
    c.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
    conn.commit()
    conn.close()


def delete_all_rules(user_id: int, inactive_only: bool = False):
    conn = get_conn()
    c = conn.cursor()
    if inactive_only:
        c.execute("DELETE FROM logs WHERE rule_id IN (SELECT id FROM rules WHERE user_id = ? AND active = 0)", (user_id,))
        c.execute("DELETE FROM rules WHERE user_id = ? AND active = 0", (user_id,))
    else:
        c.execute("DELETE FROM logs WHERE rule_id IN (SELECT id FROM rules WHERE user_id = ?)", (user_id,))
        c.execute("DELETE FROM rules WHERE user_id = ?", (user_id,))
    conn.commit()
    count = c.rowcount
    conn.close()
    return count


def get_rules_for_chat(user_id, chat_id):
    """Returns active rules matching chat_id or global (chat_id = 0).
    Ordered so specific chat rules take precedence over global rules,
    and within each level: sender -> keyword -> all."""
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        """SELECT * FROM rules
           WHERE user_id = ? AND (chat_id = ? OR chat_id = 0) AND active = 1
           ORDER BY
             CASE WHEN chat_id = 0 THEN 1 ELSE 0 END ASC,
             CASE trigger_type
               WHEN 'sender' THEN 1
               WHEN 'mention' THEN 2
               WHEN 'keyword' THEN 3
               WHEN 'all' THEN 4
               ELSE 5
             END ASC,
             id ASC""",
        (user_id, chat_id),
    )
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def chat_has_any_rule(user_id, chat_id):
    """True if this chat has an active rule specifically configured for it."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT 1 FROM rules WHERE user_id = ? AND chat_id = ? AND active = 1 LIMIT 1", (user_id, chat_id))
    row = c.fetchone()
    conn.close()
    return row is not None


# ---------- Default / away rule ----------

def upsert_default_rule(user_id, reply_text, reaction_emoji, delay_seconds, active):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT user_id FROM default_rules WHERE user_id = ?", (user_id,))
    if c.fetchone():
        c.execute(
            """UPDATE default_rules
               SET reply_text = ?, reaction_emoji = ?, delay_seconds = ?, active = ?, updated_at = ?
               WHERE user_id = ?""",
            (reply_text, reaction_emoji, delay_seconds, 1 if active else 0, time.time(), user_id),
        )
    else:
        c.execute(
            """INSERT INTO default_rules (user_id, reply_text, reaction_emoji, delay_seconds, active, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, reply_text, reaction_emoji, delay_seconds, 1 if active else 0, time.time()),
        )
    conn.commit()
    conn.close()


def get_default_rule(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM default_rules WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


# ---------- Mention rule (Groups & Channels) ----------
 
def upsert_mention_rule(user_id, reply_text, reaction_emoji, delay_seconds, cooldown_seconds, active, target_chats=None):
    conn = get_conn()
    c = conn.cursor()
    target_chats_json = json.dumps(target_chats) if isinstance(target_chats, (list, dict)) else (target_chats if target_chats else None)
    c.execute("SELECT user_id FROM mention_rules WHERE user_id = ?", (user_id,))
    if c.fetchone():
        c.execute(
            """UPDATE mention_rules
               SET reply_text = ?, reaction_emoji = ?, delay_seconds = ?, cooldown_seconds = ?, active = ?, target_chats = ?, updated_at = ?
               WHERE user_id = ?""",
            (reply_text, reaction_emoji, delay_seconds, cooldown_seconds, 1 if active else 0, target_chats_json, time.time(), user_id),
        )
    else:
        c.execute(
            """INSERT INTO mention_rules (user_id, reply_text, reaction_emoji, delay_seconds, cooldown_seconds, active, target_chats, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, reply_text, reaction_emoji, delay_seconds, cooldown_seconds, 1 if active else 0, target_chats_json, time.time()),
        )
    conn.commit()
    conn.close()


def get_mention_rule(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM mention_rules WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    res = dict(row)
    if res.get("target_chats"):
        try:
            res["target_chats"] = json.loads(res["target_chats"])
        except Exception:
            res["target_chats"] = None
    else:
        res["target_chats"] = None
    return res


# ---------- Logs ----------

def add_log(rule_id, message_snippet):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO logs (rule_id, message_snippet, replied_at) VALUES (?, ?, ?)",
        (rule_id, message_snippet[:200], time.time()),
    )
    conn.commit()
    conn.close()


def admin_overview():
    """Snapshot across every connected user — for the admin dashboard only."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT users.id, users.phone, users.display_name, users.created_at,
               (SELECT COUNT(*) FROM rules WHERE rules.user_id = users.id) as rule_count,
               (SELECT COUNT(*) FROM rules WHERE rules.user_id = users.id AND rules.active = 1) as active_rule_count,
               (SELECT COUNT(*) FROM logs
                  JOIN rules ON logs.rule_id = rules.id
                  WHERE rules.user_id = users.id) as reply_count
        FROM users
        ORDER BY users.created_at DESC
    """)
    users = [dict(r) for r in c.fetchall()]

    c.execute("SELECT COUNT(*) as total FROM logs")
    total_replies = c.fetchone()["total"]

    conn.close()
    return {
        "total_users": len(users),
        "total_replies_sent": total_replies,
        "users": users,
    }


def list_logs(user_id, limit=100):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        """SELECT logs.*, rules.chat_name, rules.reply_text FROM logs
           JOIN rules ON logs.rule_id = rules.id
           WHERE rules.user_id = ?
           ORDER BY logs.replied_at DESC LIMIT ?""",
        (user_id, limit),
    )
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows
