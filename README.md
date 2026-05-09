# AC Bridge — Agent Connect

Connects your local AI agent CLI to the **Agent Connect** platform — usable via the **Agent Talk iOS app**, Alexa skill, iOS/Siri Shortcuts, and Telegram.

The bridge runs as a systemd user service, listens for MQTT wakeup signals, calls your local CLI agent, and returns the answer to the server.

**Telegram-only mode** is also supported: run the bridge without any Agent Connect account, purely as a private Telegram bot that talks to your local AI agent.

## Requirements

- Python 3.8+
- `python3-venv` (`sudo apt install python3-venv`)
- A local AI agent with a CLI interface (e.g. Claude CLI, OpenClaw, Aider, Goose …)
- For full setup: an account at https://agent-connect.computeq.de
- For iOS App setup: the **Agent Talk** app on your iPhone
- For Telegram-only: a Telegram bot token (from [@BotFather](https://t.me/BotFather)) and your Telegram user ID

## Installation

```bash
# 1. Clone the repository
git clone https://github.com/computeq-admin/ac-bridge.git
cd ac-bridge

# 2. Install (creates venv, installs dependencies)
./install.sh
```

## Setup

### Option A — Telegram-only (no Agent Connect account required)

If you only want to chat with your local AI agent via Telegram:

```bash
python3 setup.py --telegram-only
```

This walks you through an interactive setup that asks for:

- Your username (used for the service name)
- CLI agent command and working directory
- Telegram bot token (get one from [@BotFather](https://t.me/BotFather))
- Telegram chat ID (your user ID — ask [@userinfobot](https://t.me/userinfobot))
- Optional: custom system prompt

All Claude CLI parameters are pre-filled with sensible defaults
(`--output-format json --dangerously-skip-permissions`, `--resume` for session
continuity, etc.). The service is installed and started at the end.

The service name will be `ac_bridge_tg-<username>`.

In Telegram you can start a new session at any time with the `/new` command.

### Option B — Full setup (interactive, with Agent Connect)

```bash
./start.sh --setup
```

Guides you through all steps interactively.

### Option C — Full setup (non-interactive, for AI agents)

Designed so an AI agent can set up the bridge on behalf of the user:

**Step 1 — Generate Token-A and display QR code:**
```bash
python3 setup.py --get-token
```
Output: `Token-A: <64-char hex token>` plus a QR code rendered in the terminal.

The user can either:
- **Scan the QR code** with the **Agent Talk iOS app** (Config tab → "Scan Token-A") — the app then displays a 6-digit OTT automatically, or
- Open https://agent-connect.computeq.de, go to "Set up connection", enter the Token-A manually, and receive the OTT there.

**Step 2 — Redeem the 6-digit OTT:**
```bash
python3 setup.py --redeem-ott <6-digit OTT>
```
This contacts the server, saves all credentials to `config.json`,
installs the systemd user service, and starts the bridge.

The OTT is valid for **10 minutes** and becomes invalid after the first use (or first failed attempt).

Optional flag: `--no-service` skips service installation.

**Step 3 — Configure the CLI agent:**

Use the **Agent Talk iOS app** (Config tab → Agent Configuration) or open
https://agent-connect.computeq.de and go to "Configure Bridge".
Use the preset buttons (e.g. "Claude Defaults") to fill in the parameters.
The bridge receives the updated config via MQTT automatically.

### AI agent instructions (copy-paste for your AI)

> Please install the AC Bridge on this machine:
> 1. `git clone https://github.com/computeq-admin/ac-bridge.git`
> 2. `cd ac-bridge && ./install.sh`
> 3. `python3 setup.py --get-token`
>
> Show me the QR code from the terminal. I will scan it with the Agent Talk iOS app and give you a 6-digit OTT.
> Then run: `python3 setup.py --redeem-ott <OTT>`

## CLI commands reference

| Command | Description |
|---|---|
| `python3 setup.py --telegram-only` | Interactive Telegram-only setup (no AC account needed) |
| `./start.sh --setup` | Interactive full setup (with Agent Connect) |
| `python3 setup.py --get-token` | Show / generate Token-A (non-interactive) |
| `python3 setup.py --redeem-ott <OTT>` | Redeem OTT and complete setup (non-interactive) |
| `python3 setup.py --redeem-ott <OTT> --no-service` | Same, skip systemd service install |
| `python3 setup.py --config` | Reconfigure CLI agent locally (interactive) |
| `./start.sh` | Start bridge manually (no service) |

## Configuration (config.json)

Created automatically by setup. Credentials are protected (chmod 600).

| Field | Description |
|---|---|
| `token_a` | Bridge identity token (do not change) |
| `token_b` | Rotating API token (auto-renewed on every server call) |
| `server_url` | AC server URL |
| `telegram_only` | `true` = Telegram-only mode, MQTT disabled |
| `service_name` | systemd user service name |
| `mqtt_host/port/user/password/tls` | MQTT broker credentials (from server, not used in Telegram-only mode) |
| `cli_command` | Full path to AI agent CLI (e.g. `~/.npm-global/bin/claude`) |
| `cli_working_dir` | Working directory for the CLI process |
| `cli_prompt_param` | Flag to pass the prompt (e.g. `-p`) |
| `cli_system_prompt_param` | Flag for system prompt (e.g. `--system-prompt`) |
| `cli_session_id_param` | Flag to resume a session (e.g. `--resume`) |
| `cli_session_id_output_field` | JSON field name for session ID in output (dot-notation supported) |
| `cli_answer_output_field` | JSON field name for answer text in output (dot-notation supported) |
| `cli_extra_params` | Additional CLI flags (space-separated string) |
| `cli_env` | Environment variables as JSON object |
| `cli_file_param` | Flag to pass file attachments (leave empty for Claude CLI) |
| `cli_timeout` | Timeout in seconds (default: 600) |
| `telegram_bot_token` | Telegram bot token for direct chat |
| `telegram_chat_id` | Telegram chat ID to accept messages from |
| `telegram_system_prompt` | System prompt for Telegram conversations |
| `lang` | Language for error messages: `DE` or `EN` |
| `user` | Username (used for service name in Telegram-only mode) |

## How it works

### Full mode (Agent Talk iOS app / Alexa / Siri / Telegram via Agent Connect)

```
Agent Talk iOS app / Alexa / Siri / Telegram
        ↓
   AC Server
        ↓ MQTT wakeup
   AC Bridge (this)
        ↓ HTTP pull
   fetch job from server
        ↓
   call local CLI agent
        ↓
   return answer to server
        ↓
   iOS app shows answer / Alexa reads / Telegram reply
```

### Telegram-only mode

```
Telegram message
        ↓ long-poll
   AC Bridge (this)
        ↓
   call local CLI agent
        ↓
   Telegram reply
```

## Telegram features

- **Markdown formatting**: responses are sent with Markdown enabled — code blocks, bold, and italic are rendered in Telegram. If the agent response contains characters that break Telegram's parser, the message is automatically retried as plain text.
- **Long responses**: answers longer than 4096 characters are split into multiple messages.
- **File attachments**: photos, documents, audio, and video sent to the bot are forwarded to the CLI agent.
- **Session continuity**: the agent remembers the conversation context across messages. Send `/new` to start a fresh session.

## Service management

```bash
# Status
systemctl --user status <service-name>

# Restart
systemctl --user restart <service-name>

# Logs (live)
journalctl --user -u <service-name> -f

# Enable auto-start without login session
loginctl enable-linger $USER
```

Service name conventions:
- Full setup: `ac_bridge-<email>` (e.g. `ac_bridge-user-example.com`)
- Telegram-only: `ac_bridge_tg-<username>` (e.g. `ac_bridge_tg-ingo`)

## Update

```bash
cd ~/ac-bridge   # or wherever you cloned the repo
git pull
systemctl --user restart $(grep service_name config.json | cut -d'"' -f4)
```

Or if you know the service name:
```bash
systemctl --user restart ac_bridge-user-example.com
```

## Re-setup (reset connection)

```bash
./start.sh --setup
# → choose "Re-configure" → new Token-A is generated
# → enter new Token-A in the web portal
```

For Telegram-only, just re-run `python3 setup.py --telegram-only` — existing values are shown as defaults.
