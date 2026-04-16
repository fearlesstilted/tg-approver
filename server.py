"""
Telegram Approval + Remote Control Server for Claude Code
──────────────────────────────────────────────────────────
Запуск: python server.py (через start.bat)

Команды в Telegram:
  /prompt <текст>  — отправить задачу Claude, результат придёт сюда
  /status          — что сейчас висит на аппруве + последние действия
  /cancel          — отклонить все pending запросы
  /help            — список команд
"""
import asyncio
import io
import json
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
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = int(os.environ.get("TELEGRAM_CHAT_ID", "0"))
PORT    = int(os.environ.get("APPROVER_PORT", "8877"))
# Папка проекта для /prompt (можно переопределить в .env)
PROJECT_DIR = os.environ.get("PROJECT_DIR", "D:/vsc/f/rcm_erp")

if not TOKEN or not CHAT_ID:
    sys.exit("❌ Set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID in .env")

# ── State ─────────────────────────────────────────────────────────────────────
pending: dict[str, dict] = {}           # request_id → {tool_name, tool_input, status, message_id}
recent_actions: deque = deque(maxlen=5)  # последние 5 действий для /status (из RichardAtCT паттерн)

tg_app: Application = None  # set in lifespan


# ── Форматирование сообщений ──────────────────────────────────────────────────
def _fmt(request_id: str, tool_name: str, tool_input: dict) -> str:
    """Человекочитаемое объяснение что Claude хочет сделать."""

    if tool_name == "Bash":
        cmd  = tool_input.get("command", "").strip()
        desc = tool_input.get("description", "")
        summary = desc if desc else cmd.split("\n")[0][:120]
        detail  = f"_{desc}_\n`{cmd[:300]}`" if desc else f"`{cmd[:500]}`"
        return (
            f"💻 *Bash* `[{request_id}]`\n"
            f"Хочу выполнить: {summary}\n\n"
            f"{detail}"
        )

    elif tool_name == "Write":
        path    = tool_input.get("file_path", "?")
        content = tool_input.get("content", "")
        lines   = content.count("\n") + 1
        # Полный контент — обрезка только если совсем огромный (уйдёт в файл)
        snippet = content[:3000].strip()
        return (
            f"📝 *Записать файл* `[{request_id}]`\n"
            f"Файл: `{path}`\n"
            f"Размер: {lines} строк\n\n"
            f"```\n{snippet}\n```"
        )

    elif tool_name == "Edit":
        path = tool_input.get("file_path", "?")
        old  = (tool_input.get("old_string") or "").strip()
        new  = (tool_input.get("new_string") or "").strip()
        return (
            f"✏️ *Редактировать файл* `[{request_id}]`\n"
            f"Файл: `{path}`\n\n"
            f"*Было:*\n```\n{old[:1200]}\n```\n"
            f"*Стало:*\n```\n{new[:1200]}\n```"
        )

    elif tool_name == "Agent":
        desc    = tool_input.get("description", "агент")
        prompt  = tool_input.get("prompt", "")[:300]
        subtype = tool_input.get("subagent_type", "")
        type_str = f" ({subtype})" if subtype else ""
        return (
            f"🤖 *Запустить агента{type_str}* `[{request_id}]`\n"
            f"Задача: _{desc}_\n\n"
            f"_{prompt}_"
        )

    else:
        params = "\n".join(
            f"`{k}`: {str(v)[:150]}"
            for k, v in list(tool_input.items())[:4]
        )
        return f"🔧 *{tool_name}* `[{request_id}]`\n{params}"


def _fmt_result(tool_name: str, tool_input: dict, result_text: str) -> str:
    """Краткий итог после выполнения действия (PostToolUse). Идея из RichardAtCT verbose output."""
    path = tool_input.get("file_path") or tool_input.get("command", "")[:60]
    label = {
        "Bash":  "💻 Выполнено",
        "Write": "📝 Записано",
        "Edit":  "✏️ Изменено",
        "Agent": "🤖 Агент завершил",
    }.get(tool_name, f"✅ {tool_name}")

    summary = result_text.strip()[:400] if result_text else "(нет вывода)"
    hint    = f"\n`{path}`" if path else ""
    return f"{label}{hint}\n\n```\n{summary}\n```"


# ── Telegram bot handlers ──────────────────────────────────────────────────────
async def _send_approval(request_id: str, tool_name: str, tool_input: dict):
    bot: Bot = tg_app.bot
    text = _fmt(request_id, tool_name, tool_input)
    kb   = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Allow", callback_data=f"approve:{request_id}"),
        InlineKeyboardButton("❌ Deny",  callback_data=f"deny:{request_id}"),
    ]])

    # Telegram лимит ~4096 символов. Если длиннее — шлём .txt файлом
    if len(text) > 3800:
        short = text[:800] + f"\n\n_(полный diff в файле)_"
        msg = await bot.send_message(
            chat_id=CHAT_ID, text=short,
            parse_mode="Markdown", reply_markup=kb,
        )
        # Отправляем полный текст отдельным файлом
        file_bytes = io.BytesIO(text.encode("utf-8"))
        file_bytes.name = f"{tool_name}_{request_id}.txt"
        await bot.send_document(chat_id=CHAT_ID, document=file_bytes)
    else:
        msg = await bot.send_message(
            chat_id=CHAT_ID, text=text,
            parse_mode="Markdown", reply_markup=kb,
        )
    pending[request_id]["message_id"] = msg.message_id


async def _on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    action, rid = q.data.split(":", 1)
    item = pending.get(rid)

    if not item:
        await q.edit_message_text(f"⚠️ `{rid}` не найден", parse_mode="Markdown")
        return

    if item["status"] != "pending":
        await q.answer(f"Уже {item['status']}")
        return

    if action == "approve":
        item["status"] = "approved"
        icon = "✅ Разрешено"
    else:
        item["status"] = "denied"
        icon = "❌ Отклонено"

    original = q.message.text or ""
    await q.edit_message_text(f"{original}\n\n{icon}", parse_mode="Markdown")

    # Запоминаем для /status
    recent_actions.appendleft({
        "time":      datetime.now().strftime("%H:%M"),
        "tool_name": item["tool_name"],
        "status":    item["status"],
        "rid":       rid,
    })


async def _cmd_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /prompt <текст> — запустить задачу в Claude Code (claude --print).
    Результат придёт в этот же чат.
    Идея: пользователь пишет задачу с телефона, Claude выполняет локально.
    """
    text = " ".join(ctx.args) if ctx.args else ""
    if not text:
        await update.message.reply_text("Напиши задачу: `/prompt добавь валидацию в форму`", parse_mode="Markdown")
        return

    # Без parse_mode — пользовательский текст может содержать спецсимволы Markdown
    await update.message.reply_text(f"⏳ Отправляю задачу Claude...\n\n{text}")

    try:
        # --continue продолжает последнюю сессию (сохраняет контекст разговора)
        # --print запускает без интерактивного режима
        proc = await asyncio.create_subprocess_exec(
            "claude", "--continue", "--print", text,
            cwd=PROJECT_DIR,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        output = stdout.decode("utf-8", errors="replace").strip()
        errors = stderr.decode("utf-8", errors="replace").strip()

        if not output and errors:
            output = f"[stderr]\n{errors}"
        if not output:
            output = "(нет вывода)"

        # Telegram лимит 4096 символов — режем если нужно
        if len(output) > 3800:
            output = output[:3800] + "\n\n…(обрезано)"

        await update.message.reply_text(f"✅ Готово\n\n{output}")

    except asyncio.TimeoutError:
        await update.message.reply_text("⏰ Таймаут 5 минут — Claude не ответил")
    except FileNotFoundError:
        await update.message.reply_text("❌ `claude` не найден в PATH. Запусти сервер из окружения где Claude Code установлен.")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: `{e}`", parse_mode="Markdown")


async def _cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /status — pending запросы + последние 5 действий.
    """
    lines = []

    # Pending
    active = [(rid, v) for rid, v in pending.items() if v["status"] == "pending"]
    if active:
        lines.append(f"⏳ *Ожидают аппрува: {len(active)}*")
        for rid, v in active:
            lines.append(f"  • `{rid}` — {v['tool_name']}")
    else:
        lines.append("✅ Нет pending запросов")

    # Последние действия
    if recent_actions:
        lines.append("\n*Последние действия:*")
        for a in recent_actions:
            icon = "✅" if a["status"] == "approved" else "❌"
            lines.append(f"  {icon} {a['time']} — {a['tool_name']} `[{a['rid']}]`")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def _cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /cancel — отклонить все pending запросы разом.
    """
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
        await update.message.reply_text(f"❌ Отклонено {cancelled} запросов")
    else:
        await update.message.reply_text("Нет активных запросов")


async def _cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Claude Remote Control*\n\n"
        "/prompt `<текст>` — дать задачу Claude\n"
        "/status — что ждёт аппрува + история\n"
        "/cancel — отклонить всё pending\n"
        "/help — эта справка\n\n"
        "Каждый Bash / Write / Edit / Agent придёт сюда с кнопками ✅ Allow / ❌ Deny",
        parse_mode="Markdown",
    )


# ── FastAPI lifespan ───────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global tg_app
    tg_app = Application.builder().token(TOKEN).build()
    tg_app.add_handler(CallbackQueryHandler(_on_callback))
    tg_app.add_handler(CommandHandler("prompt", _cmd_prompt))
    tg_app.add_handler(CommandHandler("status", _cmd_status))
    tg_app.add_handler(CommandHandler("cancel", _cmd_cancel))
    tg_app.add_handler(CommandHandler("help",   _cmd_help))

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
    """PostToolUse хук шлёт сюда результат выполненного действия."""
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
    """
    PostToolUse хук вызывает этот endpoint после выполнения.
    Шлём краткий итог в Telegram (паттерн из RichardAtCT verbose output).
    """
    bot: Bot = tg_app.bot
    text = _fmt_result(req.tool_name, req.tool_input, req.result)
    try:
        await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="Markdown")
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
