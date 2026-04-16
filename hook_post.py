"""
Claude Code PostToolUse hook — шлёт результат выполненного действия в Telegram.

После того как Claude выполнил Bash/Write/Edit/Agent — отправляем краткий итог
чтобы пользователь видел что реально произошло (паттерн verbose output).

Настройка в ~/.claude/settings.json:
{
  "hooks": {
    "PostToolUse": [{
      "matcher": "Bash|Write|Edit|Agent",
      "command": "python D:/vsc/f/tg_approver/hook_post.py"
    }]
  }
}
"""
import io
import json
import sys
import urllib.request

# UTF-8 на Windows
sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")

SERVER = "http://127.0.0.1:8877"


def main():
    raw = sys.stdin.read().strip()
    if not raw:
        return

    try:
        data = json.loads(raw)
    except Exception:
        return

    tool_name  = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})
    # Результат может быть строкой или структурой
    tool_result = data.get("tool_result", "")
    if isinstance(tool_result, dict):
        # Берём текстовое содержимое если есть
        tool_result = tool_result.get("output") or tool_result.get("content") or str(tool_result)

    # Отправляем только если сервер запущен — не блокируем если нет
    try:
        body = json.dumps({
            "tool_name":  tool_name,
            "tool_input": tool_input,
            "result":     str(tool_result)[:600],
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{SERVER}/result", data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        urllib.request.urlopen(req, timeout=3)
    except Exception:
        pass  # Сервер не запущен — молча пропускаем


if __name__ == "__main__":
    main()
