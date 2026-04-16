# Claude Code Telegram Approver

Remote control for Claude Code via Telegram. Every file edit, bash command, or agent call shows up in your phone with Allow/Deny buttons. Also lets you send prompts to Claude while away from your PC.

## What it does

- **Approve/Deny** every `Bash`, `Write`, `Edit`, `Agent` call before Claude executes it
- **From PC:** press `Enter` to allow, `Esc` to deny directly in terminal
- **From phone:** tap ✅ Allow or ❌ Deny in Telegram
- **`/prompt <text>`** — send a task to Claude from Telegram, get result back in chat (continues last session)
- **`/status`** — see pending requests + last 5 actions
- **`/cancel`** — deny all pending at once
- If server is not running — Claude works normally without any approval

## Setup

**1. Clone and install**
```bash
git clone https://github.com/yourname/tg-approver.git
cd tg-approver
pip install -r requirements.txt
```

**2. Create `.env`**
```bash
cp .env.example .env
# Fill in your values
```

**3. Register hooks in `~/.claude/settings.json`**
```json
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "Bash|Write|Edit|Agent",
      "command": "python /path/to/tg_approver/hook.py",
      "timeout": 620
    }],
    "PostToolUse": [{
      "matcher": "Bash|Write|Edit|Agent",
      "command": "python /path/to/tg_approver/hook_post.py",
      "timeout": 5
    }]
  }
}
```

**4. Start the server**
```bash
# Windows
start.bat

# Linux/Mac
python server.py
```

## Getting credentials

- **Bot token:** message [@BotFather](https://t.me/BotFather) → `/newbot`
- **Chat ID:** message [@userinfobot](https://t.me/userinfobot)

## Stack

Python 3.10+, FastAPI, python-telegram-bot, uvicorn
