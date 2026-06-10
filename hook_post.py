"""
Claude Code PostToolUse hook — forwards tool call results to Telegram.

After Claude executed a Bash/Write/Edit/Agent call, send a short summary
so you can see what actually happened from your phone.

Register in ~/.claude/settings.json — see README.
"""
import io
import json
import os
import sys
import urllib.request

SERVER = os.environ.get("TG_APPROVER_SERVER", "http://127.0.0.1:8877")


def main():
    if os.name == "nt":
        sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")

    raw = sys.stdin.read().strip()
    if not raw:
        return

    try:
        data = json.loads(raw)
    except Exception:
        return

    tool_name  = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})
    # Newer Claude Code versions send tool_response, older ones tool_result;
    # either may be a plain string or a structure
    tool_result = data.get("tool_response", data.get("tool_result", ""))
    if isinstance(tool_result, dict):
        tool_result = tool_result.get("output") or tool_result.get("content") or str(tool_result)

    # Only notify if the server is up — never block the session
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
        pass  # server down — silently skip


if __name__ == "__main__":
    main()
