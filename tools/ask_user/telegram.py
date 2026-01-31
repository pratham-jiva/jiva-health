"""
Ask User via Telegram - direct message + inline keyboard for fast responses.
No Claude subprocess needed. Event-driven via polling.
"""

import asyncio
import json
import urllib.request
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Optional

from .base import AskUserBase, Question, QuestionType, Answer
from .state import (
    save_pending, check_answer, clear_question, mark_answered,
)

# Auto-detect project root (tools/ask_user/telegram.py -> project root)
REPO_DIR = Path(__file__).resolve().parent.parent.parent

# Will be set by init() or from env
BOT_TOKEN: Optional[str] = None
CREATOR_CHAT_ID: Optional[int] = None


def _load_config():
    """Load bot token and chat_id from telegram_bot module or env."""
    global BOT_TOKEN, CREATOR_CHAT_ID
    if BOT_TOKEN and CREATOR_CHAT_ID:
        return

    import os
    BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
    CREATOR_CHAT_ID = int(os.environ.get("TELEGRAM_CHAT_ID", "0")) or None

    if not BOT_TOKEN or not CREATOR_CHAT_ID:
        # Fallback: read constants directly from telegram_bot.py file
        try:
            import re
            # Try project root first (jiva-health), then tools/ (pratham-home)
            bot_file = REPO_DIR / "telegram_bot.py"
            if not bot_file.exists():
                bot_file = REPO_DIR / "tools" / "telegram_bot.py"
            content = bot_file.read_text(encoding="utf-8")
            if not BOT_TOKEN:
                m = re.search(r'BOT_TOKEN\s*=\s*["\']([^"\']+)["\']', content)
                if m:
                    BOT_TOKEN = m.group(1)
            if not CREATOR_CHAT_ID:
                m = re.search(r'CREATOR_CHAT_ID\s*=\s*(\d+)', content)
                if m:
                    CREATOR_CHAT_ID = int(m.group(1))
        except Exception:
            pass

    if not BOT_TOKEN:
        raise RuntimeError("No Telegram config found. Set TELEGRAM_BOT_TOKEN env or check telegram_bot.py")


def _send_telegram(method: str, params: dict) -> dict:
    """Low-level Telegram API call."""
    _load_config()
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = json.dumps(params).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def _build_inline_keyboard(options: list[str]) -> dict:
    """Build Telegram InlineKeyboardMarkup from options list."""
    buttons = []
    for i, opt in enumerate(options):
        buttons.append([{"text": opt, "callback_data": f"ask_{i}_{opt[:60]}"}])
    return {"inline_keyboard": buttons}


def _poll_updates(last_update_id: int, timeout: int = 30) -> tuple[list, int]:
    """Long-poll Telegram for new updates. Returns (updates, new_offset)."""
    _load_config()
    params = {"offset": last_update_id + 1, "timeout": timeout, "allowed_updates": ["message", "callback_query"]}
    try:
        result = _send_telegram("getUpdates", params)
        updates = result.get("result", [])
        new_offset = last_update_id
        for u in updates:
            uid = u.get("update_id", 0)
            if uid > new_offset:
                new_offset = uid
        return updates, new_offset
    except Exception:
        return [], last_update_id


class TelegramAskUser(AskUserBase):
    """Ask user via Telegram with inline keyboards for choices."""

    def __init__(self, chat_id: int = None, bot_token: str = None):
        _load_config()
        self.chat_id = chat_id or CREATOR_CHAT_ID
        self.bot_token = bot_token or BOT_TOKEN

    async def ask(self, question: Question) -> Answer:
        """Send question via Telegram, wait for response."""
        question_id = f"q_{int(datetime.now().timestamp() * 1000)}"

        # Build message params
        params = {"chat_id": self.chat_id, "text": question.text}

        if question.question_type == QuestionType.TEXT:
            pass  # Normal message, user types freely
        elif question.question_type == QuestionType.CONFIRM:
            params["reply_markup"] = _build_inline_keyboard(["Có", "Không"])
        elif question.question_type == QuestionType.CHOICE:
            params["reply_markup"] = _build_inline_keyboard(question.options)
        elif question.question_type == QuestionType.CHECKBOX:
            # For checkbox, add instruction text
            params["text"] += "\n\n(Reply with comma-separated numbers, e.g. 1,3)"
            numbered = [f"{i+1}. {opt}" for i, opt in enumerate(question.options)]
            params["text"] += "\n" + "\n".join(numbered)

        # Send message
        result = _send_telegram("sendMessage", params)
        msg_id = result.get("result", {}).get("message_id", 0)

        # Save state for webhook handler (using shared state module)
        save_pending(
            question_id=question_id,
            text=question.text,
            question_type=question.question_type.value,
            options=question.options,
            message_id=msg_id,
        )

        # Poll for answer
        answer = await self._wait_for_answer(question_id, question, timeout=question.timeout)

        # Cleanup
        clear_question(question_id)
        return answer

    async def _wait_for_answer(self, question_id: str, question: Question, timeout: int) -> Answer:
        """Poll Telegram updates until answer received or timeout."""
        start = asyncio.get_event_loop().time()
        last_update_id = 0

        while (asyncio.get_event_loop().time() - start) < timeout:
            # Check file-based answer (webhook may have written it)
            file_answer = check_answer(question_id)
            if file_answer is not None:
                return self._parse_answer(file_answer, question)

            # Direct polling (when no webhook is running)
            updates, last_update_id = await asyncio.get_event_loop().run_in_executor(
                None, _poll_updates, last_update_id, min(5, timeout)
            )

            for update in updates:
                answer = self._extract_answer(update, question)
                if answer is not None:
                    return answer

            await asyncio.sleep(1)

        # Timeout
        return Answer(value="", answered=False)

    def _extract_answer(self, update: dict, question: Question) -> Optional[Answer]:
        """Extract answer from a Telegram update."""
        # Callback query (inline keyboard button press)
        cb = update.get("callback_query")
        if cb:
            cb_data = cb.get("data", "")
            if cb_data.startswith("ask_"):
                parts = cb_data.split("_", 2)
                choice_text = parts[2] if len(parts) > 2 else cb_data

                # Answer the callback to remove loading indicator
                try:
                    _send_telegram("answerCallbackQuery", {"callback_query_id": cb["id"]})
                except Exception:
                    pass

                return Answer(value=choice_text, answered=True, raw=cb)

        # Regular message (text reply)
        msg = update.get("message", {})
        if msg.get("chat", {}).get("id") == self.chat_id and msg.get("text"):
            return self._parse_answer(msg["text"], question)

        return None

    def _parse_answer(self, text: str, question: Question) -> Answer:
        """Parse raw text into Answer based on question type."""
        if question.question_type == QuestionType.CHECKBOX:
            # Parse comma-separated numbers
            selected = []
            for part in text.split(","):
                part = part.strip()
                if part.isdigit():
                    idx = int(part) - 1
                    if 0 <= idx < len(question.options):
                        selected.append(question.options[idx])
            return Answer(value=text, answered=True, selected=selected)

        return Answer(value=text.strip(), answered=True)

    async def notify(self, message: str) -> bool:
        """Send notification (no reply expected)."""
        try:
            result = _send_telegram("sendMessage", {"chat_id": self.chat_id, "text": message})
            return result.get("ok", False)
        except Exception:
            return False


# --- Webhook handler (called from telegram_bot.py) ---

def handle_ask_user_callback(update: dict) -> bool:
    """Handle callback_query for ask_user inline keyboards.
    Call this from telegram_bot.py webhook handler.
    Returns True if handled, False if not an ask_user callback.
    """
    cb = update.get("callback_query")
    if not cb:
        return False

    cb_data = cb.get("data", "")
    if not cb_data.startswith("ask_"):
        return False

    # Extract answer text from callback data
    parts = cb_data.split("_", 2)
    answer_text = parts[2] if len(parts) > 2 else cb_data

    # Use shared state to mark answered
    success = mark_answered(answer=answer_text)
    if success:
        # Acknowledge callback
        try:
            _send_telegram("answerCallbackQuery", {
                "callback_query_id": cb["id"],
                "text": f"Received: {answer_text}"
            })
        except Exception:
            pass
        return True

    return False


def handle_ask_user_reply(chat_id: int, text: str) -> bool:
    """Handle text reply for pending ask_user question.
    Call this from telegram_bot.py message handler.
    Returns True if handled, False if no pending question.
    """
    return mark_answered(answer=text)
