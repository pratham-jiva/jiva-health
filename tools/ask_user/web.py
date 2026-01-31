"""
Ask User via Web - HTTP endpoint for browser-based Q&A.
Works with existing Telegram webhook server (port 8443) or standalone.

Architecture:
  1. Agent calls ask() → saves question to state file + returns question_id
  2. Web client polls GET /api/ask_user/pending → gets current question
  3. User answers in browser → POST /api/ask_user/answer
  4. Agent's polling loop picks up answer from state file

State file: soul/ask_user_web_state.json (separate from Telegram state)
"""

import asyncio
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from .base import AskUserBase, Question, QuestionType, Answer

REPO_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = REPO_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
WEB_STATE_FILE = DATA_DIR / "ask_user_web_state.json"


def _read_state() -> dict:
    """Read web ask_user state file."""
    if not WEB_STATE_FILE.exists():
        return {}
    try:
        return json.loads(WEB_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_state(state: dict):
    """Write web ask_user state file."""
    WEB_STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


class WebAskUser(AskUserBase):
    """Ask user via web browser. Uses file-based IPC with HTTP endpoints."""

    def __init__(self, poll_interval: float = 1.0):
        self.poll_interval = poll_interval

    async def ask(self, question: Question) -> Answer:
        """Save question to state file, wait for web client to answer."""
        question_id = f"web_{uuid.uuid4().hex[:8]}"

        # Save pending question
        state = _read_state()
        state[question_id] = {
            "text": question.text,
            "type": question.question_type.value,
            "options": question.options,
            "default": question.default,
            "asked_at": datetime.now().isoformat(),
            "answered": False,
            "answer": None,
        }
        _write_state(state)

        # Poll for answer
        answer = await self._wait_for_answer(question_id, question)

        # Cleanup
        state = _read_state()
        state.pop(question_id, None)
        _write_state(state)

        return answer

    async def _wait_for_answer(self, question_id: str, question: Question) -> Answer:
        """Poll state file until answer received or timeout."""
        start = asyncio.get_event_loop().time()
        timeout = question.timeout

        while (asyncio.get_event_loop().time() - start) < timeout:
            state = _read_state()
            q = state.get(question_id, {})
            if q.get("answered"):
                raw_answer = q.get("answer", "")
                return self._parse_answer(raw_answer, question)

            await asyncio.sleep(self.poll_interval)

        return Answer(value="", answered=False)

    def _parse_answer(self, text: str, question: Question) -> Answer:
        """Parse answer based on question type."""
        if question.question_type == QuestionType.CHECKBOX:
            # Expect JSON array or comma-separated values
            selected = []
            try:
                selected = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                for part in str(text).split(","):
                    part = part.strip()
                    if part.isdigit():
                        idx = int(part) - 1
                        if 0 <= idx < len(question.options):
                            selected.append(question.options[idx])
                    elif part in question.options:
                        selected.append(part)
            return Answer(value=text, answered=True, selected=selected)

        return Answer(value=str(text).strip(), answered=True)

    async def notify(self, message: str) -> bool:
        """Save notification to state file (web client polls for it)."""
        state = _read_state()
        notif_id = f"notif_{uuid.uuid4().hex[:8]}"
        state[notif_id] = {
            "type": "notification",
            "text": message,
            "created_at": datetime.now().isoformat(),
            "read": False,
        }
        _write_state(state)
        return True


# --- HTTP Handler functions (integrate into existing server) ---

def get_pending_questions() -> list[dict]:
    """Get all pending (unanswered) questions for web client.
    Call from GET /api/ask_user/pending handler.
    """
    state = _read_state()
    pending = []
    for qid, q in state.items():
        if q.get("type") == "notification":
            continue
        if not q.get("answered"):
            pending.append({
                "id": qid,
                "text": q["text"],
                "type": q["type"],
                "options": q.get("options", []),
                "default": q.get("default"),
                "asked_at": q.get("asked_at"),
            })
    return pending


def submit_answer(question_id: str, answer: str) -> bool:
    """Submit answer for a pending question.
    Call from POST /api/ask_user/answer handler.

    Args:
        question_id: The question ID from get_pending_questions()
        answer: The answer text (or JSON for checkbox)

    Returns:
        True if answered successfully, False if question not found
    """
    state = _read_state()
    if question_id not in state:
        return False

    q = state[question_id]
    if q.get("answered"):
        return False  # Already answered

    q["answered"] = True
    q["answer"] = answer
    q["answered_at"] = datetime.now().isoformat()
    _write_state(state)
    return True


def get_notifications(mark_read: bool = True) -> list[dict]:
    """Get unread notifications for web client.
    Call from GET /api/ask_user/notifications handler.
    """
    state = _read_state()
    notifs = []
    changed = False
    for nid, n in state.items():
        if n.get("type") != "notification":
            continue
        if not n.get("read"):
            notifs.append({
                "id": nid,
                "text": n["text"],
                "created_at": n.get("created_at"),
            })
            if mark_read:
                n["read"] = True
                changed = True
    if changed:
        _write_state(state)
    return notifs
