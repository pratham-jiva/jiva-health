"""
Abstract base for ask_user tool.
Any channel (Telegram, Web, CLI) implements this interface.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


class QuestionType(Enum):
    TEXT = "text"           # Free-form text answer
    CHOICE = "choice"       # Single select from options
    CONFIRM = "confirm"     # Yes/No
    CHECKBOX = "checkbox"   # Multi-select from options


@dataclass
class Question:
    text: str
    question_type: QuestionType = QuestionType.TEXT
    options: list[str] = field(default_factory=list)
    default: Optional[str] = None
    timeout: int = 300  # seconds


@dataclass
class Answer:
    value: str                          # Raw answer text
    answered: bool = True               # False if timed out
    selected: list[str] = field(default_factory=list)  # For checkbox
    raw: Optional[dict] = None          # Channel-specific data


class AskUserBase(ABC):
    """Abstract interface - any channel implements this."""

    @abstractmethod
    async def ask(self, question: Question) -> Answer:
        """Send question, wait for answer. Returns Answer."""
        ...

    @abstractmethod
    async def notify(self, message: str) -> bool:
        """Send one-way notification (no answer expected)."""
        ...

    async def ask_text(self, text: str, timeout: int = 300) -> Answer:
        """Shortcut: ask free-form text question."""
        return await self.ask(Question(text=text, timeout=timeout))

    async def ask_choice(self, text: str, options: list[str], timeout: int = 300) -> Answer:
        """Shortcut: ask single-choice question."""
        return await self.ask(Question(
            text=text,
            question_type=QuestionType.CHOICE,
            options=options,
            timeout=timeout,
        ))

    async def ask_confirm(self, text: str, timeout: int = 300) -> Answer:
        """Shortcut: ask yes/no question."""
        return await self.ask(Question(
            text=text,
            question_type=QuestionType.CONFIRM,
            options=["Yes", "No"],
            timeout=timeout,
        ))

    async def ask_checkbox(self, text: str, options: list[str], timeout: int = 300) -> Answer:
        """Shortcut: ask multi-select question."""
        return await self.ask(Question(
            text=text,
            question_type=QuestionType.CHECKBOX,
            options=options,
            timeout=timeout,
        ))
