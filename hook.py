"""
Claude Code PreToolUse hook — шлёт tool call в Telegram для аппрува.
Одновременно ждёт ответа с двух сторон:
  - Telegram: кнопки ✅ Allow / ❌ Deny
  - Терминал: Enter = разрешить, Esc = отклонить (если за ПК)

Настройка в ~/.claude/settings.json:
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "Bash|Write|Edit|Agent",
      "command": "python D:/vsc/f/tg_approver/hook.py"
    }]
  }
}
"""
import io
import json
import msvcrt   # Windows: неблокирующее чтение клавиатуры
import sys
import time
import threading
import uuid
import urllib.request
import urllib.error

# UTF-8 на Windows
sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")

SERVER        = "http://127.0.0.1:8877"
POLL_INTERVAL = 2    # секунды между проверками сервера
TIMEOUT       = 600  # 10 минут максимум

# Инструменты которые всегда безопасны — пропускаем без вопросов
SAFE_TOOLS = {
    "Read", "Glob", "Grep", "LS",
    "WebFetch", "WebSearch",
    "TodoRead", "TodoWrite",
    "TaskGet", "TaskList", "TaskOutput",
    "ToolSearch", "NotebookRead",
    "ListMcpResourcesTool", "ReadMcpResourceTool",
}

# Bash команды read-only — пропускаем
SAFE_BASH_PREFIXES = (
    "git log", "git status", "git diff", "git show", "git branch",
    "python -m pytest", "pytest ",
    "ls ", "ls\n", "cat ", "head ", "tail ", "wc ",
    "echo ", "pwd", "which ", "where ",
    "grep ", "find ",
    "curl -s", "curl --silent",
)


def _deny(reason: str):
    print(json.dumps({"decision": "block", "reason": reason}))
    sys.exit(2)


def _allow():
    sys.exit(0)


def _post(path: str, data: dict) -> dict:
    body = json.dumps(data).encode("utf-8")
    req  = urllib.request.Request(
        f"{SERVER}{path}", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def _get(path: str) -> dict:
    with urllib.request.urlopen(f"{SERVER}{path}", timeout=5) as r:
        return json.loads(r.read())


def _keyboard_listener(decision: list):
    """
    Слушает клавиатуру в отдельном потоке пока ждём ответа из Telegram.
    Enter → approve, Esc → deny.
    Это даёт возможность аппрувить прямо из терминала если за ПК.
    """
    while decision[0] is None:
        try:
            if msvcrt.kbhit():
                key = msvcrt.getch()
                if key in (b'\r', b'\n'):      # Enter
                    decision[0] = "approved"
                    return
                elif key == b'\x1b':           # Esc
                    decision[0] = "denied"
                    return
        except Exception:
            pass
        time.sleep(0.05)


def main():
    raw = sys.stdin.read().strip()
    if not raw:
        _allow()

    try:
        data = json.loads(raw)
    except Exception:
        _allow()

    tool_name  = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})
    session_id = data.get("session_id", "")

    # 1. Пропускаем безопасные инструменты
    if tool_name in SAFE_TOOLS:
        _allow()

    # 2. Пропускаем безопасные Bash команды
    if tool_name == "Bash":
        cmd = tool_input.get("command", "").strip()
        if any(cmd.startswith(p) for p in SAFE_BASH_PREFIXES):
            _allow()

    # 3. Проверяем что сервер запущен
    try:
        _get("/health")
    except Exception:
        # Сервер не запущен — пропускаем с предупреждением
        sys.stderr.write(
            "[tg-approver] ⚠️  Сервер не запущен на 8877 — авто-разрешение.\n"
            "[tg-approver]    Запусти: python D:/vsc/f/tg_approver/server.py\n"
        )
        _allow()

    # 4. Отправляем запрос на аппрув в Telegram
    request_id = str(uuid.uuid4())[:8]
    try:
        _post("/pending", {
            "request_id": request_id,
            "tool_name":  tool_name,
            "tool_input": tool_input,
            "session_id": session_id,
        })
    except Exception as e:
        sys.stderr.write(f"[tg-approver] POST failed: {e}\n")
        _allow()

    # 5. Ждём ответа — из Telegram ИЛИ с клавиатуры терминала
    sys.stderr.write(
        f"[tg-approver] ⏳ [{request_id}] Ожидание... "
        f"(Enter=разрешить, Esc=отклонить, или ответь в Telegram)\n"
    )

    # Запускаем слушатель клавиатуры в отдельном потоке
    local_decision: list = [None]  # список вместо переменной — мутабельно из потока
    kb_thread = threading.Thread(target=_keyboard_listener, args=(local_decision,), daemon=True)
    kb_thread.start()

    elapsed = 0
    while elapsed < TIMEOUT:
        # Сначала проверяем локальное решение (терминал быстрее)
        if local_decision[0] == "approved":
            sys.stderr.write(f"[tg-approver] ✅ Разрешено с терминала [{request_id}]\n")
            _allow()
        elif local_decision[0] == "denied":
            sys.stderr.write(f"[tg-approver] ❌ Отклонено с терминала [{request_id}]\n")
            _deny("Отклонено с терминала")

        # Затем проверяем Telegram
        try:
            resp   = _get(f"/decision/{request_id}")
            status = resp.get("status")
            if status == "approved":
                sys.stderr.write(f"[tg-approver] ✅ Разрешено из Telegram [{request_id}]\n")
                _allow()
            elif status == "denied":
                sys.stderr.write(f"[tg-approver] ❌ Отклонено из Telegram [{request_id}]\n")
                _deny("Отклонено из Telegram")
        except Exception:
            pass

        time.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL

    _deny(f"Таймаут: нет ответа за {TIMEOUT // 60} минут")


if __name__ == "__main__":
    main()
