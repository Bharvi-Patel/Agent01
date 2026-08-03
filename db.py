import sqlite3
import json
from datetime import datetime

DB_PATH = "content_agent.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT,
            subtopic TEXT,
            content TEXT,
            status TEXT DEFAULT 'pending_review',
            feedback TEXT,
            published_url TEXT,
            created_at TEXT,
            updated_at TEXT
        )
    """)
    conn.commit()
    conn.close()

def save_draft_to_db(category, subtopic, content):
    conn = sqlite3.connect(DB_PATH)
    now = datetime.now().isoformat()
    cursor = conn.execute("""
        INSERT INTO drafts (category, subtopic, content, status, created_at, updated_at)
        VALUES (?, ?, ?, 'pending_review', ?, ?)
    """, (category, subtopic, content, now, now))
    draft_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return draft_id

def get_pending_drafts():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT id, category, subtopic, created_at FROM drafts WHERE status = 'pending_review'").fetchall()
    conn.close()
    return rows

def get_draft_content(draft_id):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT content FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    conn.close()
    return row[0] if row else None

def approve_draft(draft_id, live=False):
    conn = sqlite3.connect(DB_PATH)
    status = "approved_live" if live else "approved_draft"
    conn.execute("UPDATE drafts SET status = ?, updated_at = ? WHERE id = ?",
                 (status, datetime.now().isoformat(), draft_id))
    conn.commit()
    conn.close()
    print(f"Draft {draft_id} marked as {status}")

def reject_draft(draft_id, feedback):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE drafts SET status = 'rejected', feedback = ?, updated_at = ? WHERE id = ?",
                 (feedback, datetime.now().isoformat(), draft_id))
    conn.commit()
    conn.close()
    print(f"Draft {draft_id} rejected")

def promote_draft_to_live(draft_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE drafts SET status = 'approved_live', updated_at = ? WHERE id = ?",
                 (datetime.now().isoformat(), draft_id))
    conn.commit()
    conn.close()
    print(f"Draft {draft_id} marked as approved_live")

def publish_approved_drafts(publish_fn, clean_json_fn):
    """
    publish_fn: your real publish() function (passed in, since it lives in the notebook)
    clean_json_fn: your clean_json_string() function
    """
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT id, content FROM drafts WHERE status = 'approved_live'").fetchall()

    if not rows:
        print("No drafts ready to publish.")
        conn.close()
        return

    for draft_id, content in rows:
        payload = json.loads(clean_json_fn(content))
        payload["status"] = "live"
        result = publish_fn(payload)
        conn.execute("UPDATE drafts SET status = 'published', published_url = ?, updated_at = ? WHERE id = ?",
                     (result.get("url", ""), datetime.now().isoformat(), draft_id))
        print(f"Published draft {draft_id} -> {result}")

    conn.commit()
    conn.close()

init_db()  # runs once on import