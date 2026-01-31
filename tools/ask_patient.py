#!/usr/bin/env python3
"""
ask_patient - MCP tool server for Jiva Health

Uses TelegramAskUser to send questions directly to patient via Telegram,
and ask_user.state for IPC with telegram_bot (receiving answers).

Flow:
1. Agent calls tool ask_patient(question="...")
2. MCP server sends question directly to Telegram via TelegramAskUser
3. MCP server saves state with session_id for telegram_bot to match replies
4. User replies in Telegram -> bot writes answer via ask_user.state
5. MCP server polls state file, picks up answer, returns to agent

Session ID:
- Tu dong lay tu env var JIVA_SESSION_ID (set boi orchestrator)
- Agent KHONG can truyen session_id - chi can truyen question
"""

import json
import sys
import time
import os
import logging
from pathlib import Path

# Add project root to path so we can import tools.ask_user
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tools.ask_user.state import (
    save_pending,
    get_answered_by_session,
    get_pending_by_session,
    mark_answered,
    get_all_pending,
    cleanup_session,
    read_state,
    write_state,
)
from tools.ask_user.telegram import _send_telegram, _load_config
from datetime import datetime

logging.basicConfig(
    format="%(asctime)s - ask_patient - %(levelname)s - %(message)s",
    level=logging.INFO,
    stream=sys.stderr,
)
logger = logging.getLogger("ask_patient")

# Session ID from env (set by orchestrator MCP config)
ENV_SESSION_ID = os.environ.get("JIVA_SESSION_ID", "")

# Timeout waiting for patient response (seconds)
PATIENT_REPLY_TIMEOUT = 300  # 5 minutes

# Chat ID for patient (loaded from config)
_PATIENT_CHAT_ID = None


def _get_chat_id() -> int | None:
    """Get the patient's Telegram chat_id from env or config."""
    global _PATIENT_CHAT_ID
    if _PATIENT_CHAT_ID:
        return _PATIENT_CHAT_ID

    # First try session-specific chat_id from env (set by orchestrator)
    chat_id_str = os.environ.get("TELEGRAM_CHAT_ID", "")
    if chat_id_str:
        _PATIENT_CHAT_ID = int(chat_id_str)
        return _PATIENT_CHAT_ID

    # Fallback: load from ask_user telegram config
    try:
        _load_config()
        from tools.ask_user.telegram import CREATOR_CHAT_ID
        _PATIENT_CHAT_ID = CREATOR_CHAT_ID
        return _PATIENT_CHAT_ID
    except Exception:
        pass

    return None


# --- Public API (used by telegram_bot.py) ---

def write_question(session_id: str, question: str, options: list[str] = None) -> str:
    """Write a question to state AND send it directly via Telegram.

    Args:
        session_id: Consultation session ID for IPC matching.
        question: Question text to ask patient.
        options: Optional list of choices for inline keyboard buttons.
                 If provided, sends inline keyboard (CHOICE type).
                 If None, sends force_reply (free-form TEXT type).

    Returns question_id for tracking.
    """
    question_id = f"patient_{session_id}_{int(time.time())}"

    q_type = "choice" if options else "text"

    # Save to state file first (so telegram_bot knows there's a pending question)
    save_pending(
        question_id=question_id,
        text=question,
        question_type=q_type,
        session_id=session_id,
        options=options or [],
    )

    # Send directly to Telegram
    chat_id = _get_chat_id()
    if chat_id:
        try:
            params = {"chat_id": chat_id, "text": question}
            if options:
                # Inline keyboard with buttons
                buttons = []
                for i, opt in enumerate(options):
                    buttons.append([{"text": opt, "callback_data": f"ask_{i}_{opt[:60]}"}])
                params["reply_markup"] = {"inline_keyboard": buttons}
            _send_telegram("sendMessage", params)
            logger.info(f"Question sent to Telegram ({q_type}): {question_id}: {question[:80]}")
        except Exception as e:
            logger.error(f"Failed to send question to Telegram: {e}")
    else:
        logger.warning(f"No chat_id available, question saved to state only: {question_id}")

    return question_id


def read_answer(session_id: str, timeout: int = PATIENT_REPLY_TIMEOUT) -> str | None:
    """Poll state for answer. Called by MCP server.
    telegram_bot.py writes answer to state when user replies.
    """
    start = time.time()
    logger.info(f"Waiting for answer on session {session_id} (timeout={timeout}s)")

    while time.time() - start < timeout:
        answer = get_answered_by_session(session_id)
        if answer is not None:
            logger.info(f"Answer received for {session_id}: {(answer or '')[:80]}")
            return answer
        time.sleep(1)

    # Timeout - cleanup waiting questions for this session
    cleanup_session(session_id)
    logger.warning(f"Timeout waiting for answer on {session_id}")
    return None


def write_answer(session_id: str, answer: str) -> bool:
    """Write answer for a pending question. Called by telegram_bot."""
    success = mark_answered(session_id=session_id, answer=answer)
    if success:
        logger.info(f"Answer written for session {session_id}: {answer[:80]}")
        return True

    # No pending question found - store pre-answer for when question arrives
    logger.warning(f"No pending question for {session_id}, storing pre-answer")
    pre_id = f"pre_{session_id}_{int(time.time())}"
    save_pending(
        question_id=pre_id,
        text=None,
        question_type="text",
        session_id=session_id,
    )
    # Immediately mark it answered
    mark_answered(question_id=pre_id, answer=answer)
    return True


def check_pending_question(session_id: str) -> str | None:
    """Check if there's a pending question for this session. Called by telegram_bot."""
    return get_pending_by_session(session_id)


def get_all_pending_questions() -> list[dict]:
    """Get all pending questions across all sessions."""
    pending = get_all_pending()
    return [
        {
            "id": p["id"],
            "session_id": p.get("session_id", ""),
            "question": p.get("text", ""),
            "asked_at": p.get("asked_at", ""),
        }
        for p in pending
    ]


# --- MCP Server (stdio JSON-RPC) ---

def handle_jsonrpc(request: dict) -> dict:
    """Handle a JSON-RPC request."""
    method = request.get("method", "")
    params = request.get("params", {})
    req_id = request.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {"listChanged": False},
                },
                "serverInfo": {
                    "name": "jiva_health",
                    "version": "3.0.0",
                },
            },
        }

    elif method == "notifications/initialized":
        return None  # No response for notifications

    elif method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": [
                    {
                        "name": "ask_patient",
                        "description": (
                            "Hoi benh nhan mot cau hoi qua Telegram. "
                            "Dung khi can them thong tin de phan tich tot hon. "
                            "Benh nhan se tra loi qua Telegram va ban nhan duoc cau tra loi. "
                            "Chi hoi khi THAT SU can thiet, toi da 3 cau hoi moi lan tu van. "
                            "Session ID tu dong lay tu env - chi can truyen question. "
                            "Neu co the, truyen options de hien nut bam cho benh nhan chon nhanh."
                        ),
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "question": {
                                    "type": "string",
                                    "description": "Cau hoi can hoi benh nhan (tieng Viet, than thien, ngan gon)",
                                },
                                "options": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "Danh sach lua chon (hien nut bam inline keyboard). Neu khong truyen, benh nhan tu go text.",
                                },
                            },
                            "required": ["question"],
                        },
                    },
                ],
            },
        }

    elif method == "tools/call":
        tool_name = params.get("name", "")
        args = params.get("arguments", {})

        if tool_name == "ask_patient":
            question = args.get("question", "")
            options = args.get("options", None)
            session_id = ENV_SESSION_ID or args.get("session_id", "default")

            logger.info(f"ask_patient called: session={session_id}, question={question[:80]}, options={options}")

            if not question:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": "Error: question is required"}],
                        "isError": True,
                    },
                }

            if session_id == "default":
                logger.error("No session_id! ENV_SESSION_ID not set and no arg provided.")

            # Write question and wait for answer via ask_user.state
            write_question(session_id, question, options=options)
            answer = read_answer(session_id, timeout=PATIENT_REPLY_TIMEOUT)

            if answer is None:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{
                            "type": "text",
                            "text": "Benh nhan khong tra loi trong 5 phut. Tiep tuc voi thong tin da co.",
                        }],
                    },
                }

            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": f"Benh nhan tra loi: {answer}"}],
                },
            }

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}],
                "isError": True,
            },
        }

    # Unknown method
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def run_mcp_server():
    """Run MCP server on stdio."""
    while True:
        try:
            line = sys.stdin.readline()
            if not line:
                break

            line = line.strip()
            if not line:
                continue

            request = json.loads(line)
            response = handle_jsonrpc(request)

            if response is not None:
                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()

        except json.JSONDecodeError:
            continue
        except KeyboardInterrupt:
            break
        except Exception as e:
            sys.stderr.write(f"MCP Error: {e}\n")
            sys.stderr.flush()


if __name__ == "__main__":
    run_mcp_server()
