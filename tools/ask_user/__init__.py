"""
Ask User Tool - Multi-channel question/answer for agents.

Usage:
    # Telegram channel
    from tools.ask_user.telegram import TelegramAskUser
    asker = TelegramAskUser()
    answer = await asker.ask_text("What should I do?")

    # Web channel (HTTP endpoint)
    from tools.ask_user.web import WebAskUser
    asker = WebAskUser()
    answer = await asker.ask_choice("Pick one:", ["A", "B", "C"])

    # Auto-detect channel
    from tools.ask_user import get_asker
    asker = get_asker()  # Returns best available channel
"""

from .base import AskUserBase, Question, QuestionType, Answer


def get_asker(channel: str = "auto") -> AskUserBase:
    """Get ask_user instance for the specified channel.

    Args:
        channel: "telegram", "web", or "auto" (tries telegram first)

    Returns:
        AskUserBase implementation
    """
    if channel == "telegram":
        from .telegram import TelegramAskUser
        return TelegramAskUser()

    if channel == "web":
        from .web import WebAskUser
        return WebAskUser()

    # Auto: try telegram first (most reliable for Thay)
    if channel == "auto":
        try:
            from .telegram import TelegramAskUser
            return TelegramAskUser()
        except Exception:
            pass
        try:
            from .web import WebAskUser
            return WebAskUser()
        except Exception:
            pass

    raise RuntimeError(f"No ask_user channel available for '{channel}'")


__all__ = [
    "AskUserBase",
    "Question",
    "QuestionType",
    "Answer",
    "get_asker",
]
