"""
Jiva Health - Database Layer
SQLite database for users, patients, consultations, attachments.
"""

import sqlite3
import os
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "health.db"
UPLOADS_DIR = Path(__file__).parent / "data" / "uploads"


def _ensure_dirs():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)


def get_db() -> sqlite3.Connection:
    """Get a database connection with row_factory."""
    _ensure_dirs()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Create all tables if not exist."""
    conn = get_db()
    conn.executescript("""
        -- Telegram users
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            telegram_id INTEGER UNIQUE NOT NULL,
            telegram_username TEXT,
            full_name TEXT,
            language TEXT DEFAULT 'vi',
            created_at TEXT NOT NULL,
            last_seen TEXT NOT NULL
        );

        -- Patients (user co the hoi cho nguoi khac)
        CREATE TABLE IF NOT EXISTS patients (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            alias TEXT,
            age TEXT,
            gender TEXT,
            phone TEXT,
            medical_history TEXT,
            allergies TEXT,
            current_medications TEXT,
            notes TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        -- Consultations (benh an)
        CREATE TABLE IF NOT EXISTS consultations (
            id INTEGER PRIMARY KEY,
            consultation_id TEXT UNIQUE NOT NULL,
            user_id INTEGER NOT NULL,
            patient_id INTEGER,
            chief_complaint TEXT,
            raw_message TEXT,
            patient_profile TEXT,
            research_findings TEXT,
            status_assessment TEXT,
            causal_analysis TEXT,
            solutions TEXT,
            report TEXT,
            handoff TEXT,
            is_emergency INTEGER DEFAULT 0,
            status TEXT DEFAULT 'in_progress',
            created_at TEXT NOT NULL,
            completed_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (patient_id) REFERENCES patients(id)
        );

        -- Attachments (hinh anh, file dinh kem)
        CREATE TABLE IF NOT EXISTS attachments (
            id INTEGER PRIMARY KEY,
            consultation_id INTEGER,
            patient_id INTEGER,
            file_type TEXT NOT NULL,
            original_name TEXT,
            stored_path TEXT NOT NULL,
            mime_type TEXT,
            file_size INTEGER,
            description TEXT,
            uploaded_at TEXT NOT NULL,
            FOREIGN KEY (consultation_id) REFERENCES consultations(id),
            FOREIGN KEY (patient_id) REFERENCES patients(id)
        );

        -- Conversation messages (luu lich su hoi thoai)
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            consultation_id INTEGER,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (consultation_id) REFERENCES consultations(id)
        );
    """)
    conn.commit()
    conn.close()


# --- User operations ---

def get_or_create_user(telegram_id: int, username: str = None, full_name: str = None) -> dict:
    """Get existing user or create new one."""
    conn = get_db()
    now = datetime.now().isoformat()

    row = conn.execute(
        "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
    ).fetchone()

    if row:
        conn.execute(
            "UPDATE users SET last_seen = ?, telegram_username = COALESCE(?, telegram_username), "
            "full_name = COALESCE(?, full_name) WHERE telegram_id = ?",
            (now, username, full_name, telegram_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
    else:
        conn.execute(
            "INSERT INTO users (telegram_id, telegram_username, full_name, created_at, last_seen) "
            "VALUES (?, ?, ?, ?, ?)",
            (telegram_id, username, full_name, now, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()

    result = dict(row)
    conn.close()
    return result


def get_user_by_telegram_id(telegram_id: int) -> dict | None:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


# --- Patient operations ---

def create_patient(user_id: int, name: str, **kwargs) -> dict:
    """Create a new patient profile."""
    conn = get_db()
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO patients (user_id, name, alias, age, gender, phone, "
        "medical_history, allergies, current_medications, notes, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            user_id, name,
            kwargs.get("alias"), kwargs.get("age"), kwargs.get("gender"),
            kwargs.get("phone"), kwargs.get("medical_history"),
            kwargs.get("allergies"), kwargs.get("current_medications"),
            kwargs.get("notes"), now, now,
        ),
    )
    conn.commit()
    patient_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    row = conn.execute("SELECT * FROM patients WHERE id = ?", (patient_id,)).fetchone()
    conn.close()
    return dict(row)


def get_patients_by_user(user_id: int) -> list[dict]:
    """Get all patients for a user."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM patients WHERE user_id = ? ORDER BY updated_at DESC", (user_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_patient(patient_id: int) -> dict | None:
    conn = get_db()
    row = conn.execute("SELECT * FROM patients WHERE id = ?", (patient_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_patient(patient_id: int, **kwargs) -> dict:
    """Update patient info."""
    conn = get_db()
    updates = []
    values = []
    for key in ["name", "alias", "age", "gender", "phone",
                 "medical_history", "allergies", "current_medications", "notes"]:
        if key in kwargs:
            updates.append(f"{key} = ?")
            values.append(kwargs[key])

    if updates:
        updates.append("updated_at = ?")
        values.append(datetime.now().isoformat())
        values.append(patient_id)
        conn.execute(
            f"UPDATE patients SET {', '.join(updates)} WHERE id = ?", values
        )
        conn.commit()

    row = conn.execute("SELECT * FROM patients WHERE id = ?", (patient_id,)).fetchone()
    conn.close()
    return dict(row)


def get_default_patient(user_id: int) -> dict | None:
    """Get the 'self' patient (alias='self') or the most recently used one."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM patients WHERE user_id = ? AND alias = 'self' LIMIT 1",
        (user_id,),
    ).fetchone()
    if not row:
        row = conn.execute(
            "SELECT * FROM patients WHERE user_id = ? ORDER BY updated_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    conn.close()
    return dict(row) if row else None


# --- Consultation operations ---

def create_consultation(consultation_id: str, user_id: int, patient_id: int = None,
                        raw_message: str = None) -> dict:
    """Start a new consultation."""
    conn = get_db()
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO consultations (consultation_id, user_id, patient_id, raw_message, "
        "chief_complaint, created_at, status) VALUES (?, ?, ?, ?, ?, ?, 'in_progress')",
        (consultation_id, user_id, patient_id, raw_message, raw_message, now),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM consultations WHERE consultation_id = ?", (consultation_id,)
    ).fetchone()
    conn.close()
    return dict(row)


def update_consultation(consultation_id: str, **kwargs) -> dict:
    """Update consultation data (profile, research, report, etc.)."""
    conn = get_db()
    updates = []
    values = []
    for key in ["patient_profile", "research_findings", "status_assessment",
                 "causal_analysis", "solutions", "report", "handoff",
                 "is_emergency", "status", "completed_at", "patient_id",
                 "chief_complaint"]:
        if key in kwargs:
            updates.append(f"{key} = ?")
            values.append(kwargs[key])

    if updates:
        values.append(consultation_id)
        conn.execute(
            f"UPDATE consultations SET {', '.join(updates)} WHERE consultation_id = ?",
            values,
        )
        conn.commit()

    row = conn.execute(
        "SELECT * FROM consultations WHERE consultation_id = ?", (consultation_id,)
    ).fetchone()
    conn.close()
    return dict(row)


def get_consultation(consultation_id: str) -> dict | None:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM consultations WHERE consultation_id = ?", (consultation_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_consultations_by_user(user_id: int, limit: int = 20) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM consultations WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_consultations_by_patient(patient_id: int, limit: int = 20) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM consultations WHERE patient_id = ? ORDER BY created_at DESC LIMIT ?",
        (patient_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_consultations(limit: int = 50) -> list[dict]:
    """Get all consultations with user and patient info."""
    conn = get_db()
    rows = conn.execute("""
        SELECT c.*, u.telegram_username, u.full_name as user_name,
               p.name as patient_name
        FROM consultations c
        LEFT JOIN users u ON c.user_id = u.id
        LEFT JOIN patients p ON c.patient_id = p.id
        ORDER BY c.created_at DESC LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --- Attachment operations ---

def save_attachment(file_data: bytes, original_name: str, mime_type: str,
                    consultation_id: int = None, patient_id: int = None,
                    description: str = None) -> dict:
    """Save an attachment file and record in DB."""
    _ensure_dirs()
    now = datetime.now()
    date_dir = UPLOADS_DIR / now.strftime("%Y%m%d")
    date_dir.mkdir(exist_ok=True)

    ext = Path(original_name).suffix or _ext_from_mime(mime_type)
    stored_name = f"{now.strftime('%H%M%S')}_{original_name}"
    stored_path = date_dir / stored_name
    stored_path.write_bytes(file_data)

    file_type = _classify_file(mime_type)

    conn = get_db()
    conn.execute(
        "INSERT INTO attachments (consultation_id, patient_id, file_type, "
        "original_name, stored_path, mime_type, file_size, description, uploaded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (consultation_id, patient_id, file_type, original_name,
         str(stored_path), mime_type, len(file_data), description, now.isoformat()),
    )
    conn.commit()
    att_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    row = conn.execute("SELECT * FROM attachments WHERE id = ?", (att_id,)).fetchone()
    conn.close()
    return dict(row)


def get_attachments_by_consultation(consultation_id: int) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM attachments WHERE consultation_id = ? ORDER BY uploaded_at",
        (consultation_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_attachments_by_patient(patient_id: int) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM attachments WHERE patient_id = ? ORDER BY uploaded_at DESC",
        (patient_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --- Message operations ---

def save_message(user_id: int, role: str, content: str,
                 consultation_id: int = None) -> None:
    """Save a conversation message."""
    conn = get_db()
    conn.execute(
        "INSERT INTO messages (user_id, consultation_id, role, content, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, consultation_id, role, content, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def get_messages_by_consultation(consultation_id: int) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM messages WHERE consultation_id = ? ORDER BY created_at",
        (consultation_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_recent_messages(user_id: int, limit: int = 20) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM messages WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in reversed(rows)]


# --- Helpers ---

def _classify_file(mime_type: str) -> str:
    if not mime_type:
        return "other"
    if mime_type.startswith("image/"):
        return "image"
    if mime_type.startswith("video/"):
        return "video"
    if mime_type in ("application/pdf",):
        return "pdf"
    if mime_type.startswith("audio/"):
        return "audio"
    return "document"


def _ext_from_mime(mime_type: str) -> str:
    mapping = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "application/pdf": ".pdf",
        "video/mp4": ".mp4",
        "audio/mpeg": ".mp3",
    }
    return mapping.get(mime_type, "")


# Initialize on import
init_db()
