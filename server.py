"""
Telegram approval + remote control server for Claude Code.

Run: python server.py

Telegram commands:
  /prompt <text>  — send a task to Claude Code, result comes back to the chat
  /status         — pending approvals + recent decisions
  /cancel         — deny all pending requests
  /help           — command list
"""
import asyncio
import io
import logging
import os
import sys
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = int(os.environ.get("TELEGRAM_CHAT_ID", "0"))
PORT    = int(os.environ.get("APPROVER_PORT", "8877"))
# Working directory for /prompt tasks
PROJECT_DIR = os.environ.get("PROJECT_DIR", os.getcwd())
# Claude binary — override if `claude` is not on PATH (Windows: full path to claude.cmd)
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
# How long /prompt waits for Claude to finish, seconds
PROMPT_TIMEOUT = int(os.environ.get("PROMPT_TIMEOUT", "1800"))

if not TOKEN or not CHAT_ID:
    sys.exit("❌ Set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID in .env")

# ── State ─────────────────────────────────────────────────────────────────────
pending: dict[str, dict] = {}             # request_id → {tool_name, tool_input, status, message_id}
recent_actions: deque = deque(maxlen=10)  # last decisions shown by /status

tg_app: Application = None  # set in lifespan


# ── Message formatting ────────────────────────────────────────────────────────
def _fmt(request_id: str, tool_name: str, tool_input: dict) -> str:
    """Human-readable explanation of what Claude wants to do."""

    if tool_name == "Bash":
        cmd  = tool_input.get("command", "").strip()
        desc = tool_input.get("description", "")
        summary = desc if desc else cmd.split("\n")[0][:120]
        detail  = f"_{desc}_\n`{cmd[:300]}`" if desc else f"`{cmd[:500]}`"
        return (
            f"💻 *Bash* `[{request_id}]`\n"
            f"Wants to run: {summary}\n\n"
            f"{detail}"
        )

    elif tool_name == "Write":
        path    = tool_input.get("file_path", "?")
        content = tool_input.get("content", "")
        lines   = content.count("\n") + 1
        # Full content goes to a .txt attachment if too long, see _send_approval
        snippet = content[:3000].strip()
        return (
            f"📝 *Write file* `[{request_id}]`\n"
            f"File: `{path}`\n"
            f"Size: {lines} lines\n\n"
            f"```\n{snippet}\n```"
        )

    elif tool_name == "Edit":
        path = tool_input.get("file_path", "?")
        old  = (tool_input.get("old_string") or "").strip()
        new  = (tool_input.get("new_string") or "").strip()
        return (
            f"✏️ *Edit file* `[{request_id}]`\n"
            f"File: `{path}`\n\n"
            f"*Before:*\n```\n{old[:1200]}\n```\n"
            f"*After:*\n```\n{new[:1200]}\n```"
        )

    elif tool_name == "Agent":
        desc    = tool_input.get("description", "agent")
        prompt  = tool_input.get("prompt", "")[:300]
        subtype = tool_input.get("subagent_type", "")
        type_str = f" ({subtype})" if subtype else ""
        return (
            f"🤖 *Spawn agent{type_str}* `[{request_id}]`\n"
            f"Task: _{desc}_\n\n"
            f"_{prompt}_"
        )

    else:
        params = "\n".join(
            f"`{k}`: {str(v)[:150]}"
            for k, v in list(tool_input.items())[:4]
        )
        return f"🔧 *{tool_name}* `[{request_id}]`\n{params}"


def _fmt_result(tool_name: str, tool_input: dict, result_text: str) -> str:
    """Short summary after a tool call finished (PostToolUse)."""
    path = tool_input.get("file_path") or tool_input.get("command", "")[:60]
    label = {
        "Bash":  "💻 Executed",
        "Write": "📝 Written",
        "Edit":  "✏️ Edited",
        "Agent": "🤖 Agent finished",
    }.get(tool_name, f"✅ {tool_name}")

    summary = result_text.strip()[:400] if result_text else "(no output)"
    hint    = f"\n`{path}`" if path else ""
    return f"{label}{hint}\n\n```\n{summary}\n```"


async def _send(text: str, **kwargs):
    """Send as Markdown; tool content can break Markdown parsing, fall back to plain."""
    bot: Bot = tg_app.bot
    try:
        return await bot.send_message(
            chat_id=CHAT_ID, text=text, parse_mode="Markdown", **kwargs
        )
    except Exception:
        return await bot.send_message(chat_id=CHAT_ID, text=text, **kwargs)


# ── Telegram bot handlers ──────────────────────────────────────────────────────
async def _send_approval(request_id: str, tool_name: str, tool_input: dict):
    bot: Bot = tg_app.bot
    text = _fmt(request_id, tool_name, tool_input)
    kb   = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Allow", callback_data=f"approve:{request_id}"),
        InlineKeyboardButton("❌ Deny",  callback_data=f"deny:{request_id}"),
    ]])

    # Telegram message limit is ~4096 chars; ship the full text as a .txt file
    if len(text) > 3800:
        short = text[:800] + "\n\n_(full content attached as file)_"
        msg = await _send(short, reply_markup=kb)
        file_bytes = io.BytesIO(text.encode("utf-8"))
        file_bytes.name = f"{tool_name}_{request_id}.txt"
        await bot.send_document(chat_id=CHAT_ID, document=file_bytes)
    else:
        msg = await _send(text, reply_markup=kb)
    pending[request_id]["message_id"] = msg.message_id


async def _on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    # Only the owner chat may decide
    if update.effective_chat is None or update.effective_chat.id != CHAT_ID:
        return

    action, rid = q.data.split(":", 1)
    item = pending.get(rid)

    if not item:
        await q.edit_message_text(f"⚠️ [{rid}] not found")
        return

    if item["status"] != "pending":
        await q.answer(f"Already {item['status']}")
        return

    if action == "approve":
        item["status"] = "approved"
        icon = "✅ Allowed"
    else:
        item["status"] = "denied"
        icon = "❌ Denied"

    original = q.message.text or ""
    await q.edit_message_text(f"{original}\n\n{icon}")

    recent_actions.appendleft({
        "time":      datetime.now().strftime("%H:%M"),
        "tool_name": item["tool_name"],
        "status":    item["status"],
        "rid":       rid,
    })


async def _cmd_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /prompt <text> — run a task in Claude Code (claude --continue --print).
    The result comes back to this chat. Tool calls made by that task go
    through the same Telegram approval flow.
    """
    text = " ".join(ctx.args) if ctx.args else ""
    if not text:
        await update.message.reply_text(
            "Describe the task: `/prompt add validation to the form`",
            parse_mode="Markdown",
        )
        return

    # No parse_mode — user text may contain Markdown special characters
    await update.message.reply_text(f"⏳ Sending task to Claude...\n\n{text}")

    try:
        # --continue resumes the last session (keeps conversation context)
        # --print runs non-interactively
        proc = await asyncio.create_subprocess_exec(
            CLAUDE_BIN, "--continue", "--print", text,
            cwd=PROJECT_DIR,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=PROMPT_TIMEOUT)
        output = stdout.decode("utf-8", errors="replace").strip()
        errors = stderr.decode("utf-8", errors="replace").strip()

        if not output and errors:
            output = f"[stderr]\n{errors}"
        if not output:
            output = "(no output)"

        # Telegram message limit
        if len(output) > 3800:
            output = output[:3800] + "\n\n…(truncated)"

        await update.message.reply_text(f"✅ Done\n\n{output}")

    except asyncio.TimeoutError:
        await update.message.reply_text(
            f"⏰ Timed out after {PROMPT_TIMEOUT // 60} min — Claude did not finish"
        )
    except FileNotFoundError:
        await update.message.reply_text(
            f"❌ `{CLAUDE_BIN}` not found. Start the server from an environment "
            "where Claude Code is installed, or set CLAUDE_BIN in .env "
            "(Windows: full path to claude.cmd).",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def _cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/status — pending approvals + recent decisions."""
    lines = []

    active = [(rid, v) for rid, v in pending.items() if v["status"] == "pending"]
    if active:
        lines.append(f"⏳ *Waiting for approval: {len(active)}*")
        for rid, v in active:
            lines.append(f"  • `{rid}` — {v['tool_name']}")
    else:
        lines.append("✅ No pending requests")

    if recent_actions:
        lines.append("\n*Recent decisions:*")
        for a in recent_actions:
            icon = "✅" if a["status"] == "approved" else "❌"
            lines.append(f"  {icon} {a['time']} — {a['tool_name']} `[{a['rid']}]`")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def _cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/cancel — deny all pending requests at once."""
    cancelled = 0
    for rid, item in pending.items():
        if item["status"] == "pending":
            item["status"] = "denied"
            cancelled += 1
            recent_actions.appendleft({
                "time":      datetime.now().strftime("%H:%M"),
                "tool_name": item["tool_name"],
                "status":    "denied",
                "rid":       rid,
            })

    if cancelled:
        await update.message.reply_text(f"❌ Denied {cancelled} request(s)")
    else:
        await update.message.reply_text("No pending requests")


async def _cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Claude Code Remote Control*\n\n"
        "/prompt `<text>` — give Claude a task\n"
        "/status — pending approvals + history\n"
        "/cancel — deny everything pending\n"
        "/help — this message\n\n"
        "Every Bash / Write / Edit / Agent call lands here "
        "with ✅ Allow / ❌ Deny buttons",
        parse_mode="Markdown",
    )


# ── FastAPI lifespan ───────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global tg_app
    tg_app = Application.builder().token(TOKEN).build()

    # Commands only work from the owner chat — the bot is otherwise public
    owner = filters.Chat(chat_id=CHAT_ID)
    tg_app.add_handler(CallbackQueryHandler(_on_callback))
    tg_app.add_handler(CommandHandler("prompt", _cmd_prompt, filters=owner))
    tg_app.add_handler(CommandHandler("status", _cmd_status, filters=owner))
    tg_app.add_handler(CommandHandler("cancel", _cmd_cancel, filters=owner))
    tg_app.add_handler(CommandHandler("help",   _cmd_help,   filters=owner))

    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling(drop_pending_updates=True)

    bot_info = await tg_app.bot.get_me()
    logger.info(f"Telegram bot @{bot_info.username} started")
    logger.info(f"Approval server on http://127.0.0.1:{PORT}")
    logger.info(f"Project dir for /prompt: {PROJECT_DIR}")

    yield

    await tg_app.updater.stop()
    await tg_app.stop()
    await tg_app.shutdown()


# ── API routes ─────────────────────────────────────────────────────────────────
class PendingReq(BaseModel):
    request_id: str
    tool_name:  str
    tool_input: dict = {}
    session_id: Optional[str] = None


class ResultReq(BaseModel):
    """PostToolUse hook posts the result of an executed tool call here."""
    tool_name:  str
    tool_input: dict = {}
    result:     str  = ""


api = FastAPI(title="Claude Telegram Remote", lifespan=lifespan)


@api.post("/pending")
async def add_pending(req: PendingReq):
    pending[req.request_id] = {
        "tool_name":  req.tool_name,
        "tool_input": req.tool_input,
        "status":     "pending",
        "message_id": None,
    }
    asyncio.create_task(
        _send_approval(req.request_id, req.tool_name, req.tool_input)
    )
    return {"ok": True, "request_id": req.request_id}


@api.post("/result")
async def post_result(req: ResultReq):
    """PostToolUse hook calls this after execution; forward a short summary."""
    text = _fmt_result(req.tool_name, req.tool_input, req.result)
    try:
        await _send(text)
    except Exception as e:
        logger.warning(f"result notify failed: {e}")
    return {"ok": True}


@api.get("/decision/{request_id}")
async def get_decision(request_id: str):
    item = pending.get(request_id)
    if not item:
        return JSONResponse({"status": "not_found"}, status_code=404)
    return {"status": item["status"]}


@api.get("/health")
async def health():
    return {"ok": True, "pending_count": len(pending)}


if __name__ == "__main__":
    import io as _io
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    uvicorn.run(api, host="127.0.0.1", port=PORT, log_level="warning")
