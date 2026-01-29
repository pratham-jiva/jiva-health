"""
Jiva Health - Telegram Bot Interface
Giao tiep voi benh nhan qua Telegram.
Backend: Claude Code CLI (khong can Anthropic API key).
"""

import asyncio
import logging
import os
from pathlib import Path

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from orchestrator import HealthOrchestrator

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

# Store active consultations per user
user_sessions: dict[int, dict] = {}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    await update.message.reply_text(
        "Xin chao! Toi la Jiva Health Assistant.\n\n"
        "Toi co the giup ban tim hieu ve tinh trang suc khoe, "
        "giai thich phac do dieu tri, va chuan bi cau hoi cho lan kham tiep.\n\n"
        "Luu y: Day chi la thong tin tham khao, KHONG phai chan doan y khoa. "
        "Vui long tham khao y kien bac si.\n\n"
        "Khan cap: Goi 115 (VN) / 911 (US)\n\n"
        "Hay mo ta trieu chung hoac tinh trang cua ban:"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command."""
    await update.message.reply_text(
        "Jiva Health Assistant - Huong dan su dung\n\n"
        "Gui tin nhan mo ta trieu chung/tinh trang, vi du:\n"
        '- "Toi bi dau dau 3 ngay nay"\n'
        '- "BS ke thuoc X, muon hieu them"\n'
        '- "Ket qua xet nghiem vitamin D = 80"\n\n'
        "Lenh:\n"
        "/start - Bat dau\n"
        "/help - Huong dan\n"
        "/report - Xem bao cao moi nhat\n\n"
        "Luu y: Day chi la tham khao, khong thay the kham BS."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle patient messages - run consultation."""
    user_id = update.effective_user.id
    message = update.message.text

    if not message or len(message.strip()) < 5:
        await update.message.reply_text(
            "Vui long mo ta chi tiet hon ve trieu chung/tinh trang cua ban."
        )
        return

    # Notify processing
    processing_msg = await update.message.reply_text(
        "Dang phan tich... Vui long cho trong giay lat.\n"
        "(Qua trinh tu van gom nhieu buoc, co the mat vai phut)"
    )

    try:
        # Run consultation in thread pool (Claude CLI calls are blocking)
        orchestrator = HealthOrchestrator()
        result = await asyncio.get_event_loop().run_in_executor(
            None, orchestrator.start_consultation, message
        )

        # Store session
        user_sessions[user_id] = result

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
                text="Da hoan thanh tu van. Dung /report de xem bao cao chi tiet.",
            )

        # Send report link if web server is running
        report_path = result.get("report_path", "")
        if report_path:
            report_id = Path(report_path).stem
            await update.message.reply_text(
                f"Bao cao chi tiet: http://{WEB_HOST}:{WEB_PORT}/report/{report_id}\n\n"
                "Luu y: Thong tin chi mang tinh tham khao. "
                "Vui long tham khao bac si."
            )

    except Exception as e:
        logger.error(f"Consultation error for user {user_id}: {e}")
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=processing_msg.message_id,
            text=(
                "Xin loi, da co loi xay ra trong qua trinh tu van. "
                "Vui long thu lai sau.\n\n"
                "Neu khan cap, vui long goi 115 (VN) / 911 (US)."
            ),
        )


async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /report - show latest report."""
    user_id = update.effective_user.id
    session = user_sessions.get(user_id)

    if not session:
        await update.message.reply_text(
            "Chua co bao cao nao. Hay gui tin nhan mo ta trieu chung de bat dau tu van."
        )
        return

    report = session.get("report", "Khong co bao cao.")

    # Split long reports into chunks
    chunks = _split_text(report, 4000)
    for chunk in chunks:
        await update.message.reply_text(chunk)

    report_path = session.get("report_path", "")
    if report_path:
        report_id = Path(report_path).stem
        await update.message.reply_text(
            f"Xem tren web: http://{WEB_HOST}:{WEB_PORT}/report/{report_id}"
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


def main():
    """Start the Telegram bot."""
    if not BOT_TOKEN:
        print("ERROR: Set TELEGRAM_BOT_TOKEN environment variable")
        return

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("report", report_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Jiva Health Bot started! (Backend: Claude Code CLI)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
