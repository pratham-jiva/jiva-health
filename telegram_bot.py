"""
Jiva Health - Telegram Bot (@jivahealthbot)
Xung ho: Toi/Ban
Backend: Claude Code CLI (khong can Anthropic API key).
"""

import asyncio
import html as html_mod
import json
import logging
import os
import re
import threading
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    Defaults,
    filters,
    ContextTypes,
)

import urllib.request
import urllib.parse

from orchestrator import HealthOrchestrator
import database as db
# MCP ask_patient removed - using direct question flow instead
from tools.ask_user.telegram import handle_ask_user_callback, handle_ask_user_reply

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Telegram bot token
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

# VPS host for report links
WEB_HOST = os.getenv("WEB_HOST", "45.32.110.105")
WEB_PORT = os.getenv("WEB_PORT", "8080")

# Super admins (username -> role)
SUPER_ADMINS = {
    "passanta": "super_admin",  # Trương Hồng Hạnh
}

def md_to_tg_html(text: str) -> str:
    """Convert GitHub-flavored Markdown to Telegram HTML."""
    code_blocks = []
    def _save_code_block(m):
        lang = m.group(1) or ""
        code = html_mod.escape(m.group(2))
        code_blocks.append((lang, code))
        return f"\x00CB{len(code_blocks)-1}\x00"
    text = re.sub(r'```(\w*)\n?(.*?)```', _save_code_block, text, flags=re.DOTALL)

    inline_codes = []
    def _save_inline(m):
        code = html_mod.escape(m.group(1))
        inline_codes.append(code)
        return f"\x00IC{len(inline_codes)-1}\x00"
    text = re.sub(r'`([^`\n]+)`', _save_inline, text)

    text = html_mod.escape(text)
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text)
    text = re.sub(r'~~(.+?)~~', r'<s>\1</s>', text)
    text = re.sub(r'\[(.+?)\]\((.+?)\)', r'<a href="\2">\1</a>', text)
    text = re.sub(r'^#{1,6}\s+(.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)
    text = re.sub(r'^&gt;\s?(.+)$', r'<blockquote>\1</blockquote>', text, flags=re.MULTILINE)
    text = re.sub(r'</blockquote>\n<blockquote>', '\n', text)

    for i, (lang, code) in enumerate(code_blocks):
        if lang:
            repl = f'<pre><code class="language-{lang}">{code}</code></pre>'
        else:
            repl = f'<pre>{code}</pre>'
        text = text.replace(f'\x00CB{i}\x00', repl)
    for i, code in enumerate(inline_codes):
        text = text.replace(f'\x00IC{i}\x00', f'<code>{code}</code>')
    return text


async def send_html(target, text: str, **kwargs):
    """Send message with HTML formatting, fallback to plain text."""
    try:
        return await target.reply_text(md_to_tg_html(text), parse_mode="HTML", **kwargs)
    except Exception:
        return await target.reply_text(text, **kwargs)


async def edit_html(bot, chat_id: int, message_id: int, text: str):
    """Edit message with HTML formatting, fallback to plain text."""
    try:
        return await bot.edit_message_text(
            chat_id=chat_id, message_id=message_id,
            text=md_to_tg_html(text), parse_mode="HTML",
        )
    except Exception:
        return await bot.edit_message_text(
            chat_id=chat_id, message_id=message_id, text=text,
        )


# Active sessions: telegram_id -> {user, patient, state, orchestrator, ...}
user_sessions: dict[int, dict] = {}

# Session state persistence file (survives restarts)
SESSION_STATE_FILE = Path(__file__).parent / "data" / "active_sessions.json"


def _save_session_state(tg_id: int, state: str, consultation_id: str = ""):
    """Persist active session state to file so it survives bot restarts."""
    try:
        data = {}
        if SESSION_STATE_FILE.exists():
            data = json.loads(SESSION_STATE_FILE.read_text(encoding="utf-8"))
        data[str(tg_id)] = {"state": state, "consultation_id": consultation_id}
        SESSION_STATE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.error(f"Failed to save session state: {e}")


def _load_session_state(tg_id: int) -> dict | None:
    """Load persisted session state (after restart)."""
    try:
        if SESSION_STATE_FILE.exists():
            data = json.loads(SESSION_STATE_FILE.read_text(encoding="utf-8"))
            return data.get(str(tg_id))
    except Exception:
        pass
    return None


def _clear_session_state(tg_id: int):
    """Clear persisted session state."""
    try:
        if SESSION_STATE_FILE.exists():
            data = json.loads(SESSION_STATE_FILE.read_text(encoding="utf-8"))
            data.pop(str(tg_id), None)
            SESSION_STATE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _has_pending_ipc_question(consultation_id: str) -> bool:
    """Check if there's an active IPC question waiting for answer."""
    return check_pending_question(consultation_id) is not None


def send_typing(chat_id):
    """Send typing indicator to show bot is working."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendChatAction"
    try:
        params = {"chat_id": chat_id, "action": "typing"}
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=5):
            return True
    except Exception:
        return False


def _get_session(telegram_user) -> dict:
    """Get or create session for telegram user."""
    tg_id = telegram_user.id
    if tg_id not in user_sessions:
        full_name = telegram_user.full_name or telegram_user.first_name or ""
        username = telegram_user.username or ""
        user = db.get_or_create_user(
            telegram_id=tg_id, username=username, full_name=full_name
        )
        user_sessions[tg_id] = {
            "user": user,
            "patient": None,
            "state": "idle",
        }
        # Save identity facts from Telegram profile
        if full_name:
            db.save_user_fact(user["id"], "identity", "full_name", full_name, source="telegram")
        if username:
            db.save_user_fact(user["id"], "identity", "telegram_username", username, source="telegram")
    return user_sessions[tg_id]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    session = _get_session(update.effective_user)
    user = session["user"]

    # Auto-create 'self' patient if first time
    default_patient = db.get_default_patient(user["id"])
    if not default_patient:
        default_patient = db.create_patient(
            user_id=user["id"],
            name=user.get("full_name") or "Toi",
            alias="self",
        )
    session["patient"] = default_patient

    await update.message.reply_text(
        f"Xin chào {user.get('full_name', 'bạn')}! "
        f"Tôi là Jiva Health Assistant.\n\n"
        f"Bệnh nhân hiện tại: {default_patient['name']}\n\n"
        "Các lệnh:\n"
        "/start - Bắt đầu\n"
        "/me - Hồ sơ của tôi\n"
        "/patient - Chọn/tạo bệnh nhân\n"
        "/history - Lịch sử khám\n"
        "/report - Báo cáo gần nhất\n"
        "/help - Hướng dẫn\n\n"
        "Lưu ý: Đây chỉ là thông tin tham khảo, KHÔNG phải chẩn đoán y khoa.\n"
        "Khẩn cấp: Gọi 115 (VN) / 911 (US)\n\n"
        "Hãy mô tả triệu chứng hoặc tình trạng sức khỏe của bạn:"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command."""
    await update.message.reply_text(
        "Jiva Health Assistant - Hướng dẫn\n\n"
        "Gửi tin nhắn mô tả triệu chứng/tình trạng, ví dụ:\n"
        '- "Tôi bị đau đầu 3 ngày nay"\n'
        '- "BS kê thuốc X, muốn hiểu thêm"\n'
        '- "Kết quả xét nghiệm vitamin D = 80"\n\n'
        "Gửi hình ảnh/file: Kết quả xét nghiệm, đơn thuốc, ảnh X-quang...\n\n"
        "Lệnh:\n"
        "/start - Bắt đầu\n"
        "/me - Hồ sơ của tôi\n"
        "/patient - Chọn/tạo bệnh nhân (khi hỏi cho người khác)\n"
        "/newpatient <tên> - Tạo bệnh nhân mới nhanh\n"
        "/history - Lịch sử khám\n"
        "/report - Báo cáo gần nhất\n"
        "/help - Hướng dẫn\n\n"
        "Lưu ý: Đây chỉ là tham khảo, không thay thế khám BS."
    )


async def me_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /me - show user's own profile."""
    session = _get_session(update.effective_user)
    user = session["user"]
    patient = db.get_default_patient(user["id"])

    if not patient:
        await update.message.reply_text(
            "Chưa có hồ sơ. Gửi /start để tạo hồ sơ."
        )
        return

    consultations = db.get_consultations_by_patient(patient["id"], limit=5)

    text = (
        f"Hồ sơ của bạn:\n"
        f"- Tên: {patient['name']}\n"
        f"- Tuổi: {patient.get('age') or 'Chưa cập nhật'}\n"
        f"- Giới: {patient.get('gender') or 'Chưa cập nhật'}\n"
        f"- Tiền sử: {patient.get('medical_history') or 'Chưa cập nhật'}\n"
        f"- Dị ứng: {patient.get('allergies') or 'Chưa cập nhật'}\n"
        f"- Thuốc: {patient.get('current_medications') or 'Chưa cập nhật'}\n"
        f"\nSố lần khám: {len(consultations)}\n"
    )
    if consultations:
        text += "\nKhám gần đây:\n"
        for c in consultations[:3]:
            complaint = (c.get("chief_complaint") or "")[:50]
            text += f"- {c['created_at'][:10]}: {complaint}\n"

    await update.message.reply_text(text)


async def patient_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /patient - choose which patient to consult for."""
    session = _get_session(update.effective_user)
    user = session["user"]
    patients = db.get_patients_by_user(user["id"])

    if not patients:
        await update.message.reply_text(
            "Chưa có bệnh nhân nào. Dùng /newpatient <tên> để tạo mới."
        )
        return

    buttons = []
    for p in patients:
        label = p["name"]
        if p.get("alias") == "self":
            label += " (tôi)"
        buttons.append([InlineKeyboardButton(label, callback_data=f"select_patient:{p['id']}")])

    buttons.append([InlineKeyboardButton("+ Tạo bệnh nhân mới", callback_data="new_patient")])

    await update.message.reply_text(
        "Chọn bệnh nhân để tư vấn:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def newpatient_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /newpatient <name> - create new patient quickly."""
    session = _get_session(update.effective_user)
    user = session["user"]

    name = " ".join(context.args) if context.args else ""
    if not name:
        await update.message.reply_text(
            "Dùng: /newpatient <tên bệnh nhân>\n"
            "Ví dụ: /newpatient Nguyễn Văn A"
        )
        return

    patient = db.create_patient(user_id=user["id"], name=name)
    session["patient"] = patient

    await update.message.reply_text(
        f"Đã tạo hồ sơ bệnh nhân: {name}\n"
        f"Các tin nhắn tiếp theo sẽ tư vấn cho {name}.\n"
        "Mô tả triệu chứng:"
    )


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /history - show consultation history."""
    session = _get_session(update.effective_user)
    user = session["user"]

    consultations = db.get_consultations_by_user(user["id"], limit=10)
    if not consultations:
        await update.message.reply_text("Chưa có lịch sử khám nào.")
        return

    text = "Lịch sử khám:\n\n"
    for c in consultations:
        status_icon = "V" if c["status"] == "completed" else "..."
        complaint = (c.get("chief_complaint") or "N/A")[:60]
        text += f"[{status_icon}] {c['created_at'][:16]}\n    {complaint}\n"
        if c["status"] == "completed":
            text += f"    Xem: http://{WEB_HOST}:{WEB_PORT}/report/{c['consultation_id']}\n"
        text += "\n"

    await update.message.reply_text(text)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline button callbacks."""
    query = update.callback_query
    data = query.data

    # Handle ask_user/ask_patient inline keyboard callbacks
    if data and (data.startswith("ask_") or data.startswith("iv_")):
        # Extract answer text from callback data: "ask_{index}_{text}"
        parts = data.split("_", 2)
        answer_text = parts[2] if len(parts) > 2 else data

        # Try ask_user state first (generic ask_user questions)
        handled = handle_ask_user_callback({"callback_query": {
            "id": query.id,
            "data": data,
            "from": {"id": update.effective_user.id},
        }})
        if handled:
            await query.answer()
            # Update the question message to show selected answer
            try:
                await query.edit_message_text(
                    f"{query.message.text}\n\n>> {answer_text}\n\nĐang xử lý..."
                )
            except Exception:
                pass
            return

        # Handle interview button answer
        tg_id = update.effective_user.id
        session = user_sessions.get(tg_id, {})
        if session.get("state") == "waiting_interview_reply":
            # Treat button click as text answer, forward to handle_message
            await query.answer()
            try:
                await query.edit_message_text(
                    f"{query.message.text}\n\n>> {answer_text}"
                )
            except Exception:
                pass
            # Process as interview reply
            await _handle_interview_reply(update, context, session, answer_text)
            return

        await query.answer()
        return

    await query.answer()

    session = _get_session(update.effective_user)

    if data.startswith("select_patient:"):
        patient_id = int(data.split(":")[1])
        patient = db.get_patient(patient_id)
        if patient:
            session["patient"] = patient
            await query.edit_message_text(
                f"Đã chọn bệnh nhân: {patient['name']}\n"
                "Mô tả triệu chứng để bắt đầu tư vấn."
            )
    elif data == "new_patient":
        session["state"] = "waiting_patient_name"
        await query.edit_message_text(
            "Nhập tên bệnh nhân mới:"
        )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle patient messages - interactive consultation with interview.

    The consultant agent handles interview autonomously via MCP ask_patient tool.
    This handler:
    1. Starts orchestrator in a background thread
    2. Polls IPC files to forward agent questions to user via Telegram
    3. Routes user replies back to the agent via IPC
    """
    session = _get_session(update.effective_user)
    user = session["user"]
    message = update.message.text

    if not message or len(message.strip()) < 3:
        await update.message.reply_text(
            "Vui lòng mô tả chi tiết hơn."
        )
        return

    # Try ask_user reply handler first (for direct ask_user.telegram questions)
    if handle_ask_user_reply(update.effective_chat.id, message):
        return  # Handled by ask_user

    # Handle special states
    if session.get("state") == "waiting_patient_name":
        patient = db.create_patient(user_id=user["id"], name=message.strip())
        session["patient"] = patient
        session["state"] = "idle"
        await update.message.reply_text(
            f"Đã tạo hồ sơ: {patient['name']}\n"
            "Mô tả triệu chứng để bắt đầu tư vấn."
        )
        return

    # Handle interview reply - user is answering questions from consultant
    if session.get("state") == "waiting_interview_reply":
        await _handle_interview_reply(update, context, session, message)
        return

    # Handle clarification reply - agent asked for more info during analysis
    if session.get("state") == "clarifying":
        await _handle_clarification_reply(update, context, session, message)
        return

    # Ensure we have a patient
    if not session.get("patient"):
        default = db.get_default_patient(user["id"])
        if not default:
            default = db.create_patient(
                user_id=user["id"],
                name=user.get("full_name") or "Toi",
                alias="self",
            )
        session["patient"] = default

    patient = session["patient"]

    # Save user message
    db.save_message(user_id=user["id"], role="user", content=message)

    # Send typing indicator immediately
    send_typing(update.effective_chat.id)

    # Show initial processing message
    processing_msg = await update.message.reply_text(
        f"Đang tiếp nhận thông tin từ {patient['name']}...\n"
        "🔍 Kiểm tra khẩn cấp..."
    )

    chat_id = update.effective_chat.id
    msg_id = processing_msg.message_id
    loop = asyncio.get_event_loop()

    total_steps = 8  # emergency, interview, research, eval, causes, solutions, synthesis, handoff
    step_counter = {"n": 0}

    def _on_step_start(step_name: str, step_desc: str):
        step_counter["n"] += 1
        n = step_counter["n"]
        icon = STEP_ICONS.get(step_name, "⏳")
        progress_bar = ">" * n + "." * (total_steps - n)
        text = (
            f"Đang tư vấn cho {patient['name']}...\n"
            f"[{progress_bar}] {n}/{total_steps}\n"
            f"{icon} {step_desc}..."
        )
        try:
            send_typing(chat_id)
            asyncio.run_coroutine_threadsafe(
                context.bot.edit_message_text(
                    chat_id=chat_id, message_id=msg_id, text=text
                ),
                loop,
            ).result(timeout=5)
        except Exception:
            pass

    try:
        # Pre-generate consultation_id so IPC polling matches the session
        from datetime import datetime as _dt
        consultation_id = f"c_{_dt.now().strftime('%Y%m%d_%H%M%S')}"

        # Create orchestrator with pre-set consultation_id and chat_id
        orchestrator = HealthOrchestrator(
            user_id=user["id"],
            patient_id=patient["id"],
            on_step_start=_on_step_start,
            consultation_id=consultation_id,
            chat_id=chat_id,
        )

        # Run orchestrator (fast: emergency check + first Claude call only)
        session["orchestrator"] = orchestrator
        session["consultation_id"] = consultation_id

        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: orchestrator.start_intake(message, consultation_id=consultation_id)
        )

        if result.get("emergency"):
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id, text=result["message"],
            )
            return

        if result.get("needs_interview"):
            # Consultant needs more info - send questions to patient
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass

            # Send greeting if any
            greeting = result.get("interview_greeting", "")
            questions_text = result.get("interview_questions", "")

            # Parse questions and send with inline keyboard if OPTIONS available
            interview_msg = ""
            if greeting:
                interview_msg += greeting + "\n\n"

            # Parse questions: lines with OPTIONS: become inline keyboard
            questions_lines = [l.strip() for l in questions_text.split("\n") if l.strip()]
            keyboard_buttons = []
            plain_questions = []

            for line in questions_lines:
                if "OPTIONS:" in line:
                    q_part, opts_part = line.split("OPTIONS:", 1)
                    plain_questions.append(q_part.strip())
                    options = [o.strip() for o in opts_part.strip().split("|") if o.strip()]
                    for i, opt in enumerate(options):
                        keyboard_buttons.append([{
                            "text": opt,
                            "callback_data": f"iv_{i}_{opt[:50]}"
                        }])
                else:
                    plain_questions.append(line)

            # Build message with numbered questions
            for i, q in enumerate(plain_questions, 1):
                interview_msg += f"{i}. {q}\n"

            # Send with or without keyboard
            if keyboard_buttons and len(plain_questions) == 1:
                # Single question with options - use inline keyboard
                from telegram import InlineKeyboardMarkup
                await update.message.reply_text(
                    interview_msg.strip(),
                    reply_markup=InlineKeyboardMarkup(keyboard_buttons),
                )
            else:
                # Multiple questions or no options - plain text
                await update.message.reply_text(interview_msg.strip())

            session["state"] = "waiting_interview_reply"
            session["orchestrator"] = orchestrator
            session["consultation_id"] = result.get("consultation_id", consultation_id)
            session["original_message"] = message
            session["interview_questions"] = questions_text
            _save_session_state(update.effective_user.id, "waiting_interview_reply", consultation_id)
            return

        if result.get("needs_clarification"):
            # Agent needs more info from patient during analysis
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass

            question = result["clarification_question"]
            step = result.get("clarification_step", "")
            step_desc = HealthOrchestrator.STEP_NAMES.get(step, step)
            await update.message.reply_text(
                f"[{step_desc}]\n{question}"
            )

            session["state"] = "clarifying"
            session["orchestrator"] = orchestrator
            session["consultation_id"] = result.get("consultation_id", consultation_id)
            return

        # Full result ready
        session["state"] = "idle"
        session.pop("orchestrator", None)
        _clear_session_state(update.effective_user.id)
        await _send_consultation_result(update, context, result, chat_id, msg_id, patient)

    except Exception as e:
        logger.error(f"Consultation error for user {user['id']}: {e}", exc_info=True)
        session["state"] = "idle"
        session.pop("orchestrator", None)
        _clear_session_state(update.effective_user.id)
        error_text = (
            "Xin lỗi, đã có lỗi xảy ra trong quá trình tư vấn. "
            "Vui lòng thử lại sau.\n\n"
            "Nếu khẩn cấp, vui lòng gọi 115 (VN) / 911 (US)."
        )
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id, text=error_text,
            )
        except Exception:
            # Progress message may have been deleted - send a new one
            try:
                await context.bot.send_message(chat_id=chat_id, text=error_text)
            except Exception:
                pass


STEP_ICONS = {
    "emergency_check": "🔍",
    "intake": "📋",
    "interview": "💬",
    "research": "📚",
    "eval": "🔬",
    "causes": "🔎",
    "solutions": "💊",
    "synthesis": "📝",
    "handoff": "✅",
}



async def _handle_interview_reply(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    session: dict, message: str
) -> None:
    """Handle patient's reply to interview questions."""
    user = session["user"]
    patient = session.get("patient", {})
    orchestrator = session.get("orchestrator")

    if not orchestrator:
        session["state"] = "idle"
        await update.message.reply_text(
            "Phien tu van bi gian doan. Vui long gui lai mo ta trieu chung."
        )
        return

    # Save answer to DB
    db.save_message(user_id=user["id"], role="user", content=message)

    # Show processing indicator
    send_typing(update.effective_chat.id)
    processing_msg = await update.message.reply_text(
        "Cảm ơn! Đang phân tích thông tin..."
    )

    chat_id = update.effective_chat.id
    msg_id = processing_msg.message_id
    loop = asyncio.get_event_loop()
    total_steps = 7
    step_counter = {"n": 0}

    def _on_step_start(step_name: str, step_desc: str):
        step_counter["n"] += 1
        n = step_counter["n"]
        icon = STEP_ICONS.get(step_name, "")
        progress_bar = ">" * n + "." * (total_steps - n)
        text = (
            f"Đang phân tích cho {patient.get('name', 'bệnh nhân')}...\n"
            f"[{progress_bar}] {n}/{total_steps}\n"
            f"{icon} {step_desc}..."
        )
        try:
            send_typing(chat_id)
            asyncio.run_coroutine_threadsafe(
                context.bot.edit_message_text(
                    chat_id=chat_id, message_id=msg_id, text=text
                ),
                loop,
            ).result(timeout=5)
        except Exception:
            pass

    orchestrator.on_step_start = _on_step_start
    original_message = session.get("original_message", "")

    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: orchestrator.continue_with_answers(original_message, message)
        )

        session["state"] = "idle"
        session.pop("orchestrator", None)
        _clear_session_state(update.effective_user.id)

        if result.get("needs_clarification"):
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass
            question = result["clarification_question"]
            step = result.get("clarification_step", "")
            step_desc = HealthOrchestrator.STEP_NAMES.get(step, step)
            await update.message.reply_text(f"[{step_desc}]\n{question}")
            session["state"] = "clarifying"
            session["orchestrator"] = orchestrator
            return

        await _send_consultation_result(update, context, result, chat_id, msg_id, patient)

    except Exception as e:
        logger.error(f"Interview continuation error: {e}", exc_info=True)
        session["state"] = "idle"
        session.pop("orchestrator", None)
        _clear_session_state(update.effective_user.id)
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=msg_id,
            text="Xin lỗi, đã có lỗi. Vui lòng thử lại.\nKhẩn cấp: Gọi 115 (VN) / 911 (US).",
        )


async def _handle_clarification_reply(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    session: dict, message: str
) -> None:
    """Handle user's reply to a clarification question from an analysis agent."""
    user = session["user"]
    patient = session["patient"]
    orchestrator = session.get("orchestrator")

    if not orchestrator:
        session["state"] = "idle"
        await update.message.reply_text(
            "Phiên tư vấn bị gián đoạn. Vui lòng gửi lại mô tả triệu chứng."
        )
        return

    # Show typing + processing indicator
    send_typing(update.effective_chat.id)
    processing_msg = await update.message.reply_text("Cảm ơn! Đang tiếp tục phân tích...")

    chat_id = update.effective_chat.id
    msg_id = processing_msg.message_id
    loop = asyncio.get_event_loop()
    total_steps = 7
    step_counter = {"current": 0}

    def _on_step_start(step_name: str, step_desc: str):
        step_counter["current"] += 1
        n = step_counter["current"]
        icon = STEP_ICONS.get(step_name, "")
        progress_bar = ">" * n + "." * (total_steps - n)
        text = (
            f"Đang phân tích cho {patient['name']}...\n"
            f"[{progress_bar}] {n}/{total_steps}\n"
            f"{icon} {step_desc}..."
        )
        try:
            send_typing(chat_id)
            asyncio.run_coroutine_threadsafe(
                context.bot.edit_message_text(
                    chat_id=chat_id, message_id=msg_id, text=text
                ),
                loop,
            ).result(timeout=5)
        except Exception:
            pass

    orchestrator.on_step_start = _on_step_start

    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None, orchestrator.continue_after_clarification, message
        )

        if result.get("needs_interview"):
            # Consultant needs more info - send questions to patient
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass

            # Send greeting if any
            greeting = result.get("interview_greeting", "")
            questions_text = result.get("interview_questions", "")

            # Parse questions and send with inline keyboard if OPTIONS available
            interview_msg = ""
            if greeting:
                interview_msg += greeting + "\n\n"

            # Parse questions: lines with OPTIONS: become inline keyboard
            questions_lines = [l.strip() for l in questions_text.split("\n") if l.strip()]
            keyboard_buttons = []
            plain_questions = []

            for line in questions_lines:
                if "OPTIONS:" in line:
                    q_part, opts_part = line.split("OPTIONS:", 1)
                    plain_questions.append(q_part.strip())
                    options = [o.strip() for o in opts_part.strip().split("|") if o.strip()]
                    for i, opt in enumerate(options):
                        keyboard_buttons.append([{
                            "text": opt,
                            "callback_data": f"iv_{i}_{opt[:50]}"
                        }])
                else:
                    plain_questions.append(line)

            # Build message with numbered questions
            for i, q in enumerate(plain_questions, 1):
                interview_msg += f"{i}. {q}\n"

            # Send with or without keyboard
            if keyboard_buttons and len(plain_questions) == 1:
                # Single question with options - use inline keyboard
                from telegram import InlineKeyboardMarkup
                await update.message.reply_text(
                    interview_msg.strip(),
                    reply_markup=InlineKeyboardMarkup(keyboard_buttons),
                )
            else:
                # Multiple questions or no options - plain text
                await update.message.reply_text(interview_msg.strip())

            session["state"] = "waiting_interview_reply"
            session["orchestrator"] = orchestrator
            session["consultation_id"] = result.get("consultation_id", consultation_id)
            session["original_message"] = message
            session["interview_questions"] = questions_text
            _save_session_state(update.effective_user.id, "waiting_interview_reply", consultation_id)
            return

        if result.get("needs_clarification"):
            # Another agent also needs more info
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass

            question = result["clarification_question"]
            step = result.get("clarification_step", "")
            step_desc = HealthOrchestrator.STEP_NAMES.get(step, step)
            await update.message.reply_text(
                f"[{step_desc}]\n{question}"
            )
            # Stay in clarifying state
            return

        # Analysis complete
        session["state"] = "idle"
        session.pop("orchestrator", None)
        await _send_consultation_result(update, context, result, chat_id, msg_id, patient)

    except Exception as e:
        logger.error(f"Clarification continuation error: {e}")
        session["state"] = "idle"
        session.pop("orchestrator", None)
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=msg_id,
            text=(
                "Xin lỗi, đã có lỗi xảy ra. Vui lòng thử lại.\n\n"
                "Neu khan cap: Goi 115 (VN) / 911 (US)."
            ),
        )


async def _send_consultation_result(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    result: dict, chat_id: int, msg_id: int, patient: dict
) -> None:
    """Send the final consultation result to user."""
    # Send handoff (short summary) with HTML formatting
    handoff = result.get("handoff", "")
    if handoff:
        if len(handoff) > 4000:
            handoff = handoff[:4000] + "\n\n... (xem báo cáo đầy đủ qua web)"
        await edit_html(context.bot, chat_id, msg_id, handoff)
    else:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=msg_id,
            text="Đã hoàn thành tư vấn. Dùng /report để xem báo cáo chi tiết.",
        )

    # Send report link
    report_path = result.get("report_path", "")
    if report_path:
        report_id = Path(report_path).stem
        await update.message.reply_text(
            f"Báo cáo chi tiết:\n"
            f"http://{WEB_HOST}:{WEB_PORT}/report/{report_id}\n\n"
            f"Bệnh nhân: {patient['name']}\n"
            "Lưu ý: Thông tin chỉ mang tính tham khảo."
        )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photo uploads (ket qua xet nghiem, don thuoc, X-quang...)."""
    session = _get_session(update.effective_user)
    user = session["user"]

    if not session.get("patient"):
        default = db.get_default_patient(user["id"])
        if not default:
            default = db.create_patient(
                user_id=user["id"],
                name=user.get("full_name") or "Toi",
                alias="self",
            )
        session["patient"] = default

    patient = session["patient"]

    # Download photo
    photo = update.message.photo[-1]  # Highest resolution
    file = await context.bot.get_file(photo.file_id)
    file_data = await file.download_as_bytearray()

    # Save attachment
    original_name = f"photo_{photo.file_unique_id}.jpg"
    caption = update.message.caption or ""

    attachment = db.save_attachment(
        file_data=bytes(file_data),
        original_name=original_name,
        mime_type="image/jpeg",
        patient_id=patient["id"],
        description=caption,
    )

    if session.get("state") == "waiting_interview_reply":
        await update.message.reply_text(
            f"Đã lưu ảnh vào hồ sơ {patient['name']}.\n"
            f"{'Mô tả: ' + caption if caption else ''}\n\n"
            "Tôi đã ghi nhận. Bạn có thể tiếp tục trả lời câu hỏi phía trên."
        )
    else:
        await update.message.reply_text(
            f"Đã lưu ảnh vào hồ sơ {patient['name']}.\n"
            f"{'Mô tả: ' + caption if caption else ''}\n\n"
            "Gửi thêm thông tin hoặc mô tả triệu chứng để bắt đầu tư vấn."
        )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle document uploads (PDF, etc.)."""
    session = _get_session(update.effective_user)
    user = session["user"]

    if not session.get("patient"):
        default = db.get_default_patient(user["id"])
        if not default:
            default = db.create_patient(
                user_id=user["id"],
                name=user.get("full_name") or "Toi",
                alias="self",
            )
        session["patient"] = default

    patient = session["patient"]

    # Download document
    doc = update.message.document
    file = await context.bot.get_file(doc.file_id)
    file_data = await file.download_as_bytearray()

    attachment = db.save_attachment(
        file_data=bytes(file_data),
        original_name=doc.file_name or f"doc_{doc.file_unique_id}",
        mime_type=doc.mime_type or "application/octet-stream",
        patient_id=patient["id"],
        description=update.message.caption or "",
    )

    if session.get("state") == "waiting_interview_reply":
        await update.message.reply_text(
            f"Đã lưu file '{doc.file_name}' vào hồ sơ {patient['name']}.\n\n"
            "Tôi đã ghi nhận. Bạn có thể tiếp tục trả lời câu hỏi phía trên."
        )
    else:
        await update.message.reply_text(
            f"Đã lưu file '{doc.file_name}' vào hồ sơ {patient['name']}.\n\n"
            "Gửi thêm thông tin hoặc mô tả triệu chứng để bắt đầu tư vấn."
        )


async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /report - show latest report."""
    session = _get_session(update.effective_user)
    user = session["user"]

    consultations = db.get_consultations_by_user(user["id"], limit=1)
    if not consultations:
        await update.message.reply_text(
            "Chưa có báo cáo nào. Gửi tin nhắn mô tả triệu chứng để bắt đầu."
        )
        return

    latest = consultations[0]
    if latest.get("handoff"):
        chunks = _split_text(latest["handoff"], 4000)
        for chunk in chunks:
            await send_html(update.message, chunk)

    if latest.get("consultation_id"):
        await update.message.reply_text(
            f"Xem đầy đủ: http://{WEB_HOST}:{WEB_PORT}/report/{latest['consultation_id']}"
        )


def _is_super_admin(telegram_user) -> bool:
    """Check if user is a super admin."""
    username = (telegram_user.username or "").lower()
    return username in SUPER_ADMINS


async def kb_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /kb - knowledge base management (super admin only)."""
    if not _is_super_admin(update.effective_user):
        await update.message.reply_text(
            "Bạn không có quyền sử dụng lệnh này."
        )
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "Quản lý Knowledge Base (Super Admin)\n\n"
            "Lệnh:\n"
            "/kb add <nội dung> - Thêm kiến thức mới\n"
            "/kb list - Xem danh sách kiến thức\n"
            "/kb clear - Xóa tất cả (cẩn thận!)\n\n"
            "Ví dụ:\n"
            '/kb add Vitamin D liều 5000 IU/ngày an toàn cho người trưởng thành'
        )
        return

    action = args[0].lower()
    content = " ".join(args[1:]) if len(args) > 1 else ""

    if action == "add" and content:
        db.add_knowledge(content, added_by=update.effective_user.username)
        await update.message.reply_text(f"Đã thêm vào knowledge base:\n{content}")
    elif action == "list":
        entries = db.get_knowledge()
        if entries:
            text = "Knowledge Base:\n\n"
            for i, e in enumerate(entries, 1):
                text += f"{i}. {e['content'][:100]}\n   (bởi @{e.get('added_by', '?')} - {e['created_at'][:10]})\n\n"
            await update.message.reply_text(text[:4000])
        else:
            await update.message.reply_text("Knowledge base trống.")
    elif action == "clear":
        db.clear_knowledge()
        await update.message.reply_text("Đã xóa toàn bộ knowledge base.")
    else:
        await update.message.reply_text("Lệnh không hợp lệ. Dùng /kb để xem hướng dẫn.")


def _split_text(text: str, max_len: int = 4000) -> list[str]:
    """Split text into chunks for Telegram."""
    if len(text) <= max_len:
        return [text]

    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip()
    return chunks


def _start_web_server():
    """Start Flask web server in a separate thread."""
    try:
        from web import app as flask_app
        host = WEB_HOST if WEB_HOST != "45.32.110.105" else "0.0.0.0"
        port = int(WEB_PORT)
        logger.info(f"Web server starting on {host}:{port}")
        flask_app.run(host=host, port=port, debug=False, use_reloader=False)
    except Exception as e:
        logger.error(f"Web server failed: {e}")


def main():
    """Start the Telegram bot and web server."""
    if not BOT_TOKEN:
        print("ERROR: Set TELEGRAM_BOT_TOKEN environment variable")
        return

    # Start web server in background thread
    web_thread = threading.Thread(target=_start_web_server, daemon=True)
    web_thread.start()

    defaults = Defaults(do_quote=False)
    app = Application.builder().token(BOT_TOKEN).defaults(defaults).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("me", me_cmd))
    app.add_handler(CommandHandler("patient", patient_cmd))
    app.add_handler(CommandHandler("newpatient", newpatient_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("report", report_cmd))
    app.add_handler(CommandHandler("kb", kb_cmd))

    # Callbacks (inline buttons)
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    logger.info("Jiva Health Bot (@jivahealthbot) started!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
