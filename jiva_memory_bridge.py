#!/usr/bin/env python3
"""
Jiva Memory Bridge - Sync jiva-health data to Jiva core memory (5W1H).

Sau mỗi consultation, ghi vào memory.db (Jiva core):
- interactions: consultation event (5W1H)
- contact_facts: health facts về người dùng
- memories: consultation learning/insight

Usage:
    from jiva_memory_bridge import sync_consultation_to_core
    sync_consultation_to_core(consultation_data, user_data)
"""

import sys
import json
from pathlib import Path
from datetime import datetime

# Jiva core tools path
JIVA_CORE_TOOLS = Path("/home/jiva/pratham-home/tools")
sys.path.insert(0, str(JIVA_CORE_TOOLS))

try:
    from memory_db import add_memory
    from conversation_db import get_connection as get_core_conn, init_db as init_core_db
    HAS_CORE = True
except ImportError:
    HAS_CORE = False


def _ensure_contact(conn, telegram_id: int, name: str = None, username: str = None) -> int:
    """Ensure contact exists in core memory, return contact_id."""
    row = conn.execute(
        "SELECT id FROM contacts WHERE telegram_id = ?", (str(telegram_id),)
    ).fetchone()

    if row:
        conn.execute(
            "UPDATE contacts SET last_seen_at = ?, interaction_count = interaction_count + 1 WHERE id = ?",
            (datetime.now().isoformat(), row["id"]),
        )
        conn.commit()
        return row["id"]

    # Create new contact
    conn.execute("""
        INSERT INTO contacts (name, type, telegram_id, telegram_username, relationship, trust_level)
        VALUES (?, 'human', ?, ?, 'patient', 5)
    """, (name or f"User_{telegram_id}", str(telegram_id), username or ""))
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def sync_consultation_to_core(
    consultation: dict,
    user_data: dict = None,
    patient_data: dict = None,
):
    """Sync completed consultation to Jiva core memory.

    Args:
        consultation: dict with keys: id, patient_profile, research_findings,
                      status_assessment, causal_analysis, solutions, report, handoff
        user_data: dict with telegram_id, full_name, telegram_username
        patient_data: dict with name, age, gender, etc.
    """
    if not HAS_CORE:
        return

    init_core_db()
    conn = get_core_conn()

    consultation_id = consultation.get("id", "unknown")
    now = datetime.now().isoformat()

    # 1. Ensure contact exists
    contact_id = None
    if user_data and user_data.get("telegram_id"):
        contact_id = _ensure_contact(
            conn,
            telegram_id=user_data["telegram_id"],
            name=user_data.get("full_name"),
            username=user_data.get("telegram_username"),
        )

    # 2. Log interaction (5W1H)
    profile_text = ""
    if consultation.get("patient_profile"):
        profile = consultation["patient_profile"]
        if isinstance(profile, dict):
            profile_text = profile.get("extracted", str(profile))
        else:
            profile_text = str(profile)

    # Build summary from handoff (shorter) or profile
    summary = consultation.get("handoff", "")[:500] or profile_text[:500]

    conn.execute("""
        INSERT INTO interactions
        (contact_id, direction, action, content, summary,
         channel, channel_detail, context, intent, outcome,
         project, importance, emotional_tone, language)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        contact_id,
        "inbound",
        "consultation",
        profile_text[:2000],
        summary,
        "telegram",
        "@jivahealthbot",
        f"consultation_{consultation_id}",
        "consult_health",
        "completed" if consultation.get("report") else "incomplete",
        "jiva-health",
        7,
        "neutral",
        "vi",
    ))
    conn.commit()

    # 3. Sync patient facts to contact_facts (if we know the contact)
    if contact_id and patient_data:
        facts_to_sync = [
            ("health", "name", patient_data.get("name")),
            ("health", "age", patient_data.get("age")),
            ("health", "gender", patient_data.get("gender")),
            ("health", "medical_history", patient_data.get("medical_history")),
            ("health", "allergies", patient_data.get("allergies")),
            ("health", "current_medications", patient_data.get("current_medications")),
        ]

        for category, key, value in facts_to_sync:
            if value and value != "N/A":
                conn.execute("""
                    INSERT OR REPLACE INTO contact_facts
                    (contact_id, category, fact_key, fact_value, confidence, source, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    contact_id, category, key, str(value)[:500],
                    0.8, f"jiva-health_{consultation_id}", now,
                ))
        conn.commit()

    # 4. Save consultation as learning in long-term memory
    topic = f"health_consultation_{consultation_id}"
    content_parts = []
    if consultation.get("handoff"):
        content_parts.append(f"Handoff: {consultation['handoff'][:500]}")
    if consultation.get("status_assessment"):
        content_parts.append(f"Assessment: {consultation['status_assessment'][:300]}")

    if content_parts:
        add_memory(
            mem_type="event",
            topic=topic,
            content="\n".join(content_parts),
            source="jiva-health",
            importance=6,
        )

    conn.close()


def sync_user_fact_to_core(
    telegram_id: int,
    category: str,
    fact_key: str,
    fact_value: str,
    confidence: float = 0.7,
    source: str = "jiva-health",
):
    """Sync a single user fact to Jiva core contact_facts."""
    if not HAS_CORE:
        return

    init_core_db()
    conn = get_core_conn()

    contact_id = _ensure_contact(conn, telegram_id)
    conn.execute("""
        INSERT OR REPLACE INTO contact_facts
        (contact_id, category, fact_key, fact_value, confidence, source, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        contact_id, category, fact_key, fact_value,
        confidence, source, datetime.now().isoformat(),
    ))
    conn.commit()
    conn.close()
