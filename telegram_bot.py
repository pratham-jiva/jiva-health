"""
Jiva Health - Telegram Bot (@jivahealthbot)
Xung ho: Toi/Ban
Backend: Claude Code CLI (khong can Anthropic API key).
"""

import asyncio
import logging
import os
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
    filters,
    ContextTypes,
)

from orchestrator import HealthOrchestrator
import database as db

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

# Active sessions: telegram_id -> {user, patient, state}
user_sessions: dict[int, dict] = {}


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
    await query.answer()

    session = _get_session(update.effective_user)
    data = query.data

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
    """Handle patient messages - run consultation."""
    session = _get_session(update.effective_user)
    user = session["user"]
    message = update.message.text

    if not message or len(message.strip()) < 3:
        await update.message.reply_text(
            "Vui lòng mô tả chi tiết hơn."
        )
        return

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

    # Notify processing
    processing_msg = await update.message.reply_text(
        f"Đang tư vấn cho {patient['name']}...\n"
        "(Gồm nhiều bước phân tích, vui lòng chờ)"
    )

    try:
        # Run consultation in thread pool (Claude CLI calls are blocking)
        orchestrator = HealthOrchestrator(
            user_id=user["id"], patient_id=patient["id"]
        )
        result = await asyncio.get_event_loop().run_in_executor(
            None, orchestrator.start_consultation, message
        )

        if result.get("emergency"):
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=processing_msg.message_id,
                text=result["message"],
            )
            return

        # Send handoff (short summary) first
        handoff = result.get("handoff", "")
        if handoff:
            if len(handoff) > 4000:
                handoff = handoff[:4000] + "\n\n... (xem bao cao day du qua web)"

            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=processing_msg.message_id,
                text=handoff,
            )
        else:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=processing_msg.message_id,
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

    except Exception as e:
        logger.error(f"Consultation error for user {user['id']}: {e}")
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=processing_msg.message_id,
            text=(
                "Xin lỗi, đã có lỗi xảy ra trong quá trình tư vấn. "
                "Vui lòng thử lại sau.\n\n"
                "Nếu khẩn cấp, vui lòng gọi 115 (VN) / 911 (US)."
            ),
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
            await update.message.reply_text(chunk)

    if latest.get("consultation_id"):
        await update.message.reply_text(
            f"Xem đầy đủ: http://{WEB_HOST}:{WEB_PORT}/report/{latest['consultation_id']}"
        )


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

    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("me", me_cmd))
    app.add_handler(CommandHandler("patient", patient_cmd))
    app.add_handler(CommandHandler("newpatient", newpatient_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("report", report_cmd))

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
