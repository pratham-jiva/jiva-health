"""
Shared state management for ask_user package.
All channels (telegram, web, MCP ask_patient) use this shared state file.

State file: data/ask_user_state.json
Format: {question_id: {text, type, options, session_id?, answered, answer, ...}}
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

# Auto-detect project root (tools/ask_user/state.py -> project root)
REPO_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = REPO_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
ASK_STATE_FILE = DATA_DIR / "ask_user_state.json"


def read_state() -> dict:
    """Read the shared ask_user state file."""
    if not ASK_STATE_FILE.exists():
        return {}
    try:
        return json.loads(ASK_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_state(state: dict):
    """Write to the shared ask_user state file."""
    ASK_STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def save_pending(question_id: str, text: str, question_type: str = "text",
                 options: list[str] = None, message_id: int = 0,
                 session_id: str = ""):
    """Save a pending question to state file."""
    state = read_state()
    state[question_id] = {
        "text": text,
        "type": question_type,
        "options": options or [],
        "session_id": session_id,
        "message_id": message_id,
        "asked_at": datetime.now().isoformat(),
        "answered": False,
        "answer": None,
    }
    write_state(state)


def check_answer(question_id: str) -> Optional[str]:
    """Check if a specific question has been answered (non-blocking)."""
    state = read_state()
    q = state.get(question_id, {})
    if q.get("answered"):
        return q.get("answer")
    return None


def mark_answered(question_id: str = "", answer: str = "",
                  session_id: str = "") -> bool:
    """Mark a question as answered. Finds by question_id or session_id.
    Returns True if found and marked.
    """
    state = read_state()

    # Find by question_id first
    if question_id and question_id in state:
        q = state[question_id]
        if not q.get("answered"):
            q["answered"] = True
            q["answer"] = answer
            q["answered_at"] = datetime.now().isoformat()
            write_state(state)
            return True

    # Find by session_id (for MCP ask_patient)
    if session_id:
        for qid, q in state.items():
            if q.get("session_id") == session_id and not q.get("answered"):
                q["answered"] = True
                q["answer"] = answer
                q["answered_at"] = datetime.now().isoformat()
                write_state(state)
                return True

    # Find first unanswered (for generic ask_user)
    if not question_id and not session_id:
        for qid, q in state.items():
            if not q.get("answered") and q.get("text"):
                q["answered"] = True
                q["answer"] = answer
                q["answered_at"] = datetime.now().isoformat()
                write_state(state)
                return True

    return False


def clear_question(question_id: str):
    """Remove a question from state."""
    state = read_state()
    state.pop(question_id, None)
    write_state(state)


def get_pending_by_session(session_id: str) -> Optional[str]:
    """Get pending question text for a session_id."""
    state = read_state()
    for qid, q in state.items():
        if q.get("session_id") == session_id and not q.get("answered"):
            return q.get("text")
    return None


def get_answered_by_session(session_id: str) -> Optional[str]:
    """Get and consume answered question for a session. Returns answer text."""
    state = read_state()
    for qid, q in state.items():
        if q.get("session_id") == session_id and q.get("answered"):
            answer = q.get("answer", "")
            state.pop(qid, None)
            write_state(state)
            return answer
    return None


def get_all_pending() -> list[dict]:
    """Get all pending (unanswered) questions."""
    state = read_state()
    pending = []
    for qid, q in state.items():
        if not q.get("answered") and q.get("text"):
            pending.append({
                "id": qid,
                "session_id": q.get("session_id", ""),
                "text": q.get("text", ""),
                "type": q.get("type", "text"),
                "options": q.get("options", []),
                "asked_at": q.get("asked_at", ""),
            })
    return pending


def cleanup_session(session_id: str):
    """Remove all unanswered questions for a session (on timeout)."""
    state = read_state()
    to_remove = [qid for qid, q in state.items()
                 if q.get("session_id") == session_id and not q.get("answered")]
    for qid in to_remove:
        state.pop(qid, None)
    if to_remove:
        write_state(state)
