# tg-approver

**Approve or deny every Claude Code action from your phone.**

A Telegram remote control for [Claude Code](https://claude.com/claude-code), built on its native hooks. Start a long task, walk away from your desk — every `Bash`, `Write`, `Edit`, and `Agent` call lands in your Telegram with two buttons:

```
💻 Bash [a3f8c2d1]
Wants to run: Push backend to production

  git push render main

      [ ✅ Allow ]   [ ❌ Deny ]
```

You can also send Claude new tasks from your phone with `/prompt`, and get the results back in chat.

No cloud, no database, no Docker. Two hook scripts, one ~400-line local server, your bot token. That's the whole thing.

## Why

Claude Code stops and waits when it needs permission for a risky action. If you're away from the keyboard, the session just sits there. tg-approver forwards that decision to your phone, so long autonomous sessions don't stall — and you keep a veto over every command instead of running with permissions disabled.

## How it works

```
Claude Code ── PreToolUse hook ──→ localhost:8877 ──→ Telegram message
     ▲                                   │                  │
     │         polls /decision           │     tap Allow/Deny
     └───────────────────────────────────┴──────────────────┘
```

- The **PreToolUse hook** intercepts the tool call, sends it to the local server, and polls for your decision. Approval and denial use Claude Code's `permissionDecision` output, so a Telegram tap is a real permission grant — no second prompt in the terminal.
- The **server** runs the Telegram bot and holds pending requests in memory.
- The **PostToolUse hook** sends a short summary after each executed action, so you see what actually happened.
- Read-only tools (`Read`, `Grep`, `git status`, `pytest`, ...) pass through automatically.
- **Fail-safe:** if the server isn't running, the hooks stay silent and Claude Code behaves exactly as it would without them.

## Quickstart

**1. Clone and install**

```bash
git clone https://github.com/fearlesstilted/tg-approver.git
cd tg-approver
pip install -r requirements.txt
```

**2. Create your bot and `.env`**

- Bot token: message [@BotFather](https://t.me/BotFather) → `/newbot`
- Your chat ID: message [@userinfobot](https://t.me/userinfobot)

```bash
cp .env.example .env   # then fill in TELEGRAM_TOKEN and TELEGRAM_CHAT_ID
```

**3. Register the hooks in `~/.claude/settings.json`**

```json
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "Bash|Write|Edit|Agent",
      "hooks": [{ "type": "command", "command": "python /absolute/path/to/tg-approver/hook.py", "timeout": 620 }]
    }],
    "PostToolUse": [{
      "matcher": "Bash|Write|Edit|Agent",
      "hooks": [{ "type": "command", "command": "python /absolute/path/to/tg-approver/hook_post.py", "timeout": 5 }]
    }]
  }
}
```

**4. Start the server**

```bash
python server.py     # Linux / macOS
start.bat            # Windows
```

Open a Claude Code session and ask it to do something. Your phone buzzes.

## Telegram commands

| Command | What it does |
|---|---|
| `/prompt <text>` | Run a task in Claude Code (`--continue`, keeps session context). Tool calls from that task go through the same approval flow. |
| `/status` | Pending approvals + last 10 decisions |
| `/cancel` | Deny everything pending |
| `/help` | Command list |

Approving from the keyboard also works while a request is pending: **Enter** = allow, **Esc** = deny (Windows out of the box; on Linux/macOS set `TG_APPROVER_TTY=1`, see below).

## Configuration

All optional, via `.env` (server) or environment (hooks):

| Variable | Default | Meaning |
|---|---|---|
| `APPROVER_PORT` | `8877` | Local server port |
| `PROJECT_DIR` | server's cwd | Working directory for `/prompt` tasks |
| `CLAUDE_BIN` | `claude` | Claude binary, if not on PATH (Windows: full path to `claude.cmd`) |
| `PROMPT_TIMEOUT` | `1800` | Seconds `/prompt` waits for Claude to finish |
| `TG_APPROVER_SERVER` | `http://127.0.0.1:8877` | Where the hooks look for the server |
| `TG_APPROVER_TIMEOUT` | `600` | Seconds the hook waits for your decision |
| `TG_APPROVER_ON_TIMEOUT` | `ask` | `ask` = fall back to the terminal prompt, `deny` = reject |
| `TG_APPROVER_TTY` | off | `1` enables Enter/Esc terminal keys on Linux/macOS (reads `/dev/tty`, can compete with the Claude Code UI for keystrokes) |

## Security notes

- The server binds to `127.0.0.1` only — nothing is exposed to the network.
- Bot commands and buttons only work from your `TELEGRAM_CHAT_ID`; anyone else messaging the bot is ignored.
- The design is fail-open by choice: server down → Claude Code's normal permission prompts apply, your session is never bricked.
- Keep your `.env` out of git (it already is, via `.gitignore`).

## FAQ

**How is this different from the Claude Code web/mobile apps?**
Those run sessions in Anthropic's cloud or mirror specific sessions. tg-approver bolts onto whatever local session you already have, in any terminal, with one decision surface for all of it — and it's ~600 lines of Python you can read in ten minutes.

**There are other Telegram bridges for Claude Code.**
Yes. Most of them wrap the whole conversation in Telegram. This one deliberately does less: it's a permission remote, not a chat client. The session stays in your terminal; only the decisions and results travel.

**What happens if I never answer?**
After `TG_APPROVER_TIMEOUT` (default 10 min) the request falls back to the normal terminal prompt (or is denied, if you set `TG_APPROVER_ON_TIMEOUT=deny`).

**Does `/prompt` bypass approvals?**
No — tasks started from Telegram trigger the same PreToolUse hook, so their actions come back to you for approval too.

## License

[MIT](LICENSE)
