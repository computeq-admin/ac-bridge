# AC Bridge — Agent Connect

Connects your local AI agent CLI to the **Agent Connect** platform — usable via Alexa skill, iOS/Siri Shortcuts, and Telegram.

The bridge runs as a systemd user service, listens for MQTT wakeup signals, calls your local CLI agent, and returns the answer to the server.

## Requirements

- Python 3.8+
- `python3-venv` (`sudo apt install python3-venv`)
- A local AI agent with a CLI interface (e.g. Claude CLI, OpenClaw, Aider, Goose …)
- An account at https://agent-connect.computeq.de

## Installation

```bash
# 1. Clone the repository
git clone https://github.com/computeq-admin/ac-bridge.git
cd ac-bridge

# 2. Install (creates venv, installs dependencies)
./install.sh
```

## Setup

### Option A — Interactive (for humans)

```bash
./start.sh --setup
```

Guides you through all steps interactively.

### Option B — Non-interactive (for AI agents)

Designed so an AI agent can set up the bridge on behalf of the user:

**Step 1 — Generate Token-A:**
```bash
python3 setup.py --get-token
```
Output: `Token-A: <64-char hex token>`

The AI should show this token to the user and ask them to open
https://agent-connect.computeq.de, go to "Set up connection",
enter the Token-A, and click "Request OTT".

**Step 2 — Redeem the OTT the user received:**
```bash
python3 setup.py --redeem-ott <OTT>
```
This contacts the server, saves all credentials to `config.json`,
installs the systemd user service, and starts the bridge.

Optional flag: `--no-service` skips service installation.

**Step 3 — Configure the CLI agent:**

Open https://agent-connect.computeq.de and go to "Configure Bridge".
Use the preset buttons (e.g. "Claude CLI Defaults") to fill in the parameters,
then click "Send config". The bridge receives the config via MQTT automatically.

### AI agent instructions (copy-paste for your AI)

> Help me set up the AC Bridge:
> 1. Clone https://github.com/computeq-admin/ac-bridge.git and run `./install.sh`
> 2. Run `python3 setup.py --get-token` and show me the Token-A
> 3. I will open https://agent-connect.computeq.de and get an OTT — tell me when you're ready
> 4. Run `python3 setup.py --redeem-ott <OTT I provide>`
> 5. The bridge is now running. I'll configure the CLI agent via the web portal.

## CLI commands reference

| Command | Description |
|---|---|
| `./start.sh --setup` | Interactive full setup |
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
| `service_name` | systemd user service name (derived from account email) |
| `mqtt_host/port/user/password/tls` | MQTT broker credentials (from server) |
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

## How it works

```
Alexa / Siri / Telegram
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
   Alexa reads answer / Telegram reply
```

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

The service name is `ac_bridge-<email>` (e.g. `ac_bridge-user-example.com`).

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
