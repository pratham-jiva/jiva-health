#!/usr/bin/env python3
"""
ask_patient - MCP tool server for Jiva Health

Cho phep Claude agent hoi benh nhan truc tiep qua Telegram.
Mechanism: file-based IPC (JSON) giua agent va telegram_bot.

Flow:
1. Agent goi tool ask_patient(question="...")
2. MCP server ghi cau hoi vao /tmp/jiva_health_ipc/ask_{session_id}.json
3. Telegram bot poll file nay, gui cau hoi cho user
4. User tra loi -> bot ghi answer vao file
5. MCP server doc answer va tra ve cho agent

Session ID:
- Tu dong lay tu env var JIVA_SESSION_ID (set boi orchestrator)
- Agent KHONG can truyen session_id - chi can truyen question

Usage as MCP server:
    python3 tools/ask_patient.py

Claude Code config (.claude/settings.json):
    "mcpServers": {
        "jiva_health": {
            "command": "python3",
            "args": ["tools/ask_patient.py"],
            "env": {"JIVA_SESSION_ID": "..."}
        }
    }
"""

import json
import sys
import time
import os
import logging
from pathlib import Path

logging.basicConfig(
    format="%(asctime)s - ask_patient - %(levelname)s - %(message)s",
    level=logging.INFO,
    stream=sys.stderr,
)
logger = logging.getLogger("ask_patient")

# IPC directory
IPC_DIR = Path("/tmp/jiva_health_ipc")
IPC_DIR.mkdir(exist_ok=True)

# Session ID from env (set by orchestrator MCP config)
ENV_SESSION_ID = os.environ.get("JIVA_SESSION_ID", "")

# Timeout waiting for patient response (seconds)
PATIENT_REPLY_TIMEOUT = 300  # 5 minutes


def get_ipc_path(session_id: str) -> Path:
    """Get IPC file path for a session."""
    return IPC_DIR / f"ask_{session_id}.json"


def write_question(session_id: str, question: str) -> Path:
    """Write a question to IPC file. Called by MCP server."""
    ipc_path = get_ipc_path(session_id)
    data = {
        "status": "waiting",
        "question": question,
        "answer": None,
        "timestamp": time.time(),
    }
    ipc_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    logger.info(f"Question written to {ipc_path.name}: {question[:80]}")
    return ipc_path


def read_answer(session_id: str, timeout: int = PATIENT_REPLY_TIMEOUT) -> str | None:
    """Poll IPC file for answer. Called by MCP server."""
    ipc_path = get_ipc_path(session_id)
    start = time.time()
    logger.info(f"Waiting for answer on {ipc_path.name} (timeout={timeout}s)")

    while time.time() - start < timeout:
        if ipc_path.exists():
            data = json.loads(ipc_path.read_text(encoding="utf-8"))
            if data.get("status") == "answered":
                answer = data["answer"]
                # Clean up
                ipc_path.unlink(missing_ok=True)
                logger.info(f"Answer received for {session_id}: {answer[:80]}")
                return answer
        time.sleep(1)  # Poll every 1 second

    # Timeout - clean up
    ipc_path.unlink(missing_ok=True)
    logger.warning(f"Timeout waiting for answer on {session_id}")
    return None


def write_answer(session_id: str, answer: str):
    """Write answer to IPC file. Called by telegram_bot."""
    ipc_path = get_ipc_path(session_id)
    if not ipc_path.exists():
        logger.warning(f"Cannot write answer - IPC file not found: {ipc_path.name}")
        return False

    data = json.loads(ipc_path.read_text(encoding="utf-8"))
    data["status"] = "answered"
    data["answer"] = answer
    data["answered_at"] = time.time()
    ipc_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    logger.info(f"Answer written to {ipc_path.name}: {answer[:80]}")
    return True


def check_pending_question(session_id: str) -> str | None:
    """Check if there's a pending question. Called by telegram_bot."""
    ipc_path = get_ipc_path(session_id)
    if not ipc_path.exists():
        return None

    data = json.loads(ipc_path.read_text(encoding="utf-8"))
    if data.get("status") == "waiting":
        return data["question"]
    return None


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
                    "version": "1.0.0",
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
                            "Session ID tu dong lay tu env - chi can truyen question."
                        ),
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "question": {
                                    "type": "string",
                                    "description": "Cau hoi can hoi benh nhan (tieng Viet, than thien, ngan gon)",
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
            # Session ID: prefer env var (reliable), fallback to arg
            session_id = ENV_SESSION_ID or args.get("session_id", "default")

            logger.info(f"ask_patient called: session={session_id}, question={question[:80]}")

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

            # Write question and wait for answer
            write_question(session_id, question)
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

            # Skip empty lines
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
