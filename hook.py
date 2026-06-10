"""
Claude Code PreToolUse hook — sends tool calls to Telegram for approval.

Waits for a decision from either side:
  - Telegram: ✅ Allow / ❌ Deny buttons
  - Terminal: Enter = allow, Esc = deny
    (Windows: always on; Linux/macOS: opt-in via TG_APPROVER_TTY=1)

If the approval server is not running, the hook stays silent and Claude Code
falls back to its normal permission prompts.

Register in ~/.claude/settings.json — see README.
"""
import io
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

SERVER        = os.environ.get("TG_APPROVER_SERVER", "http://127.0.0.1:8877")
POLL_INTERVAL = 2                                                  # seconds between server checks
TIMEOUT       = int(os.environ.get("TG_APPROVER_TIMEOUT", "600"))  # max wait, seconds
# What to do when nobody answered: "ask" → fall back to the terminal prompt,
# "deny" → reject the tool call.
ON_TIMEOUT    = os.environ.get("TG_APPROVER_ON_TIMEOUT", "ask")

# Tools that are always safe — pass through without asking
SAFE_TOOLS = {
    "Read", "Glob", "Grep", "LS",
    "WebFetch", "WebSearch",
    "TodoRead", "TodoWrite",
    "TaskGet", "TaskList", "TaskOutput",
    "ToolSearch", "NotebookRead",
    "ListMcpResourcesTool", "ReadMcpResourceTool",
}

# Read-only Bash commands — pass through
SAFE_BASH_PREFIXES = (
    "git log", "git status", "git diff", "git show", "git branch",
    "python -m pytest", "pytest ",
    "ls ", "ls\n", "cat ", "head ", "tail ", "wc ",
    "echo ", "pwd", "which ", "where ",
    "grep ", "find ",
    "curl -s", "curl --silent",
)


def _decision(decision: str, reason: str):
    """Emit a PreToolUse permission decision: allow | deny | ask."""
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def _passthrough():
    """No opinion — Claude Code's normal permission flow applies."""
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
    Listen for terminal keys in a background thread while waiting for Telegram.
    Enter → approve, Esc → deny. Lets you answer locally when you are at the PC.
    """
    if os.name == "nt":
        import msvcrt
        while decision[0] is None:
            try:
                if msvcrt.kbhit():
                    key = msvcrt.getch()
                    if key in (b"\r", b"\n"):      # Enter
                        decision[0] = "approved"
                        return
                    elif key == b"\x1b":           # Esc
                        decision[0] = "denied"
                        return
            except Exception:
                pass
            time.sleep(0.05)
        return

    # POSIX: reading /dev/tty competes with the Claude Code TUI for input,
    # so it is opt-in. Telegram buttons always work either way.
    if os.environ.get("TG_APPROVER_TTY") != "1":
        return
    try:
        import select
        import termios
        import tty
        fd = os.open("/dev/tty", os.O_RDONLY)
    except Exception:
        return
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while decision[0] is None:
            r, _, _ = select.select([fd], [], [], 0.1)
            if r:
                key = os.read(fd, 1)
                if key in (b"\r", b"\n"):
                    decision[0] = "approved"
                    return
                if key == b"\x1b":
                    decision[0] = "denied"
                    return
    except Exception:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        os.close(fd)


def main():
    if os.name == "nt":
        sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")

    raw = sys.stdin.read().strip()
    if not raw:
        _passthrough()

    try:
        data = json.loads(raw)
    except Exception:
        _passthrough()

    tool_name  = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})
    session_id = data.get("session_id", "")

    # 1. Safe tools pass without asking
    if tool_name in SAFE_TOOLS:
        _decision("allow", "Read-only tool")

    # 2. Safe read-only Bash commands pass too
    if tool_name == "Bash":
        cmd = tool_input.get("command", "").strip()
        if any(cmd.startswith(p) for p in SAFE_BASH_PREFIXES):
            _decision("allow", "Read-only command")

    # 3. Server must be up, otherwise stay out of the way
    try:
        _get("/health")
    except Exception:
        sys.stderr.write(
            "[tg-approver] ⚠️  Server not running on "
            f"{SERVER} — normal permission prompts apply.\n"
        )
        _passthrough()

    # 4. Send the approval request to Telegram
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
        _passthrough()

    # 5. Wait for an answer — Telegram OR terminal keys
    sys.stderr.write(
        f"[tg-approver] ⏳ [{request_id}] Waiting... "
        f"(answer in Telegram{' or Enter/Esc here' if os.name == 'nt' else ''})\n"
    )

    local_decision: list = [None]  # list instead of a var — mutable from the thread
    kb_thread = threading.Thread(target=_keyboard_listener, args=(local_decision,), daemon=True)
    kb_thread.start()

    elapsed = 0
    while elapsed < TIMEOUT:
        # Local decision first (faster than polling)
        if local_decision[0] == "approved":
            sys.stderr.write(f"[tg-approver] ✅ Allowed from terminal [{request_id}]\n")
            _decision("allow", "Approved from terminal")
        elif local_decision[0] == "denied":
            sys.stderr.write(f"[tg-approver] ❌ Denied from terminal [{request_id}]\n")
            _decision("deny", "Denied from terminal")

        # Then Telegram
        try:
            resp   = _get(f"/decision/{request_id}")
            status = resp.get("status")
            if status == "approved":
                sys.stderr.write(f"[tg-approver] ✅ Allowed from Telegram [{request_id}]\n")
                _decision("allow", "Approved via Telegram")
            elif status == "denied":
                sys.stderr.write(f"[tg-approver] ❌ Denied from Telegram [{request_id}]\n")
                _decision("deny", "Denied via Telegram")
        except Exception:
            pass

        time.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL

    if ON_TIMEOUT == "deny":
        _decision("deny", f"No answer within {TIMEOUT // 60} minutes")
    _decision("ask", f"No Telegram answer within {TIMEOUT // 60} minutes — ask in terminal")


if __name__ == "__main__":
    main()
