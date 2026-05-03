#!/usr/bin/env python3
"""
AC Bridge — AI Agent Tasks
Verbindet Alexa (via MQTT Wakeup) mit dem lokalen KI-Agenten (OpenWebUI / OpenAI-compatible)

Ablauf:
  1. Bridge startet, liest config.json
  2. Subscribed auf MQTT Topic: ac/{token_a}
  3. Bei Wakeup-Message: holt Job von Server (Token-B)
  4. Übergibt Prompt an den KI-Agenten
  5. Schreibt Antwort zurück an Server (Token-B rotiert)

Installation:
  pip install paho-mqtt requests

Konfiguration: config.json im gleichen Verzeichnis
"""

import json
import logging
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import paho.mqtt.client as mqtt
import requests

# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('ac_bridge.log'),
    ]
)
log = logging.getLogger('ac_bridge')

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
CONFIG_FILE = Path(__file__).parent / 'config.json'

PROTECTED_CONFIG_KEYS = {
    'token_a', 'mqtt_host', 'mqtt_port', 'mqtt_user',
    'mqtt_password', 'mqtt_tls', 'server_url', 'token_b',
    'service_name',
}

ALLOWED_CLI_EXECUTABLES = {
    'claude', 'openclaw', 'copilot', 'gemini', 'aider', 'interpreter', 'goose',
}

def load_config():
    if not CONFIG_FILE.exists():
        log.error('config.json not found. Run setup first.')
        sys.exit(1)
    with open(CONFIG_FILE) as f:
        return json.load(f)

def save_config(cfg):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(cfg, f, indent=2)

def apply_config_update(cfg):
    """Holt ausstehende Config-Aktualisierung vom Server und wendet sie lokal an.

    Wird via MQTT action=update-config ausgelöst. Der Server liefert die neuen
    Werte; protected keys werden auch bei Serverantwort nie überschrieben.
    """
    try:
        r = requests.post(
            cfg['server_url'] + '/get_config_update.php',
            json={'token_b': cfg['token_b']},
            timeout=10,
        )
        data = r.json()
    except Exception as e:
        log.error(f'get_config_update failed: {e}')
        return False

    if 'token_b_new' in data:
        cfg['token_b'] = data['token_b_new']
        save_config(cfg)

    if r.status_code == 401:
        log.error('Token-B rejected by server during config update.')
        return False

    if data.get('status') == 'no_update':
        log.info('update-config: no pending update on server.')
        return True

    if data.get('status') != 'ok':
        log.warning(f'update-config: unexpected server response: {data}')
        return False

    # cli_command Executable gegen Whitelist prüfen
    cli_command = data.get('cli_command', '')
    if cli_command:
        executable = Path(cli_command).name
        if executable not in ALLOWED_CLI_EXECUTABLES:
            log.error(
                f'update-config rejected: cli_command executable "{executable}" '
                f'is not in the allowed list {sorted(ALLOWED_CLI_EXECUTABLES)}'
            )
            return False

    updated_keys = []
    for key, value in data.items():
        if key in ('status', 'token_b_new'):
            continue
        if key in PROTECTED_CONFIG_KEYS:
            log.warning(f'update-config: server sent protected key "{key}", ignored')
        else:
            cfg[key] = value
            updated_keys.append(key)

    if updated_keys:
        save_config(cfg)
        log.info(f'Config updated successfully: {updated_keys}')
    else:
        log.info('update-config: no changes applied')

    service_name = cfg.get('service_name', '')
    if service_name and updated_keys:
        log.info(f'Restarting service: {service_name}')
        try:
            subprocess.run(
                ['systemctl', '--user', 'restart', service_name],
                timeout=10,
                check=True,
            )
        except Exception as e:
            log.error(f'Service restart failed: {e}')

    return True

# ─────────────────────────────────────────────
# Server API calls
# ─────────────────────────────────────────────
def get_job(cfg):
    """Holt offenen Job vom Server, rotiert Token-B"""
    try:
        r = requests.post(
            cfg['server_url'] + '/get_job.php',
            json={'token_b': cfg['token_b']},
            timeout=10,
        )
        data = r.json()
    except Exception as e:
        log.error(f'get_job failed: {e}')
        return None

    # Token-B immer aktualisieren
    if 'token_b_new' in data:
        cfg['token_b'] = data['token_b_new']
        save_config(cfg)

    if r.status_code == 401:
        log.error('Token-B rejected by server. Re-registration required.')
        return None

    if data.get('status') == 'no_job':
        log.info('No pending job.')
        return None

    if 'job_id' in data:
        log.info(f"Job received: #{data['job_id']}")
        return data

    log.warning(f'Unexpected get_job response: {data}')
    return None


def send_pong(cfg):
    """Antwortet auf Server-Ping, rotiert Token-B"""
    try:
        r = requests.post(
            cfg['server_url'] + '/ping.php',
            json={'token_b': cfg['token_b']},
            timeout=10,
        )
        data = r.json()
    except requests.exceptions.JSONDecodeError:
        log.error(f'send_pong: server returned no JSON (HTTP {r.status_code}): {r.text[:200]}')
        return
    except Exception as e:
        log.error(f'send_pong failed: {e}')
        return

    if 'token_b_new' in data:
        cfg['token_b'] = data['token_b_new']
        save_config(cfg)

    if data.get('status') == 'pong':
        log.info('Pong sent — bridge confirmed online')
    else:
        log.warning(f'Unexpected ping response: {data}')


def put_answer(cfg, job_id, answer):
    """Schreibt Antwort zurück, rotiert Token-B"""
    try:
        r = requests.post(
            cfg['server_url'] + '/put_answer.php',
            json={
                'token_b': cfg['token_b'],
                'job_id':  job_id,
                'answer':  answer,
            },
            timeout=10,
        )
        data = r.json()
    except Exception as e:
        log.error(f'put_answer failed: {e}')
        return False

    if 'token_b_new' in data:
        cfg['token_b'] = data['token_b_new']
        save_config(cfg)

    if data.get('status') == 'ok':
        log.info(f'Answer submitted for job #{job_id}')
        return True

    log.error(f'put_answer error: {data}')
    return False


# ─────────────────────────────────────────────
# Session management
# ─────────────────────────────────────────────
_current_session_id = None


def reset_session():
    global _current_session_id
    _current_session_id = None
    log.info('Session reset — next call starts a new session.')


def store_session_id(session_id):
    global _current_session_id
    _current_session_id = session_id
    log.info(f'Session ID stored: {session_id}')


# ─────────────────────────────────────────────
# Agent call (CLI)
# ─────────────────────────────────────────────
def call_agent_cli(cfg, prompt, system_prompt='', files=None):
    """Ruft den KI-Agenten als lokalen CLI-Prozess auf, gibt Antwort-Text zurück.

    Session-Handling:
      - Kein _current_session_id → kein --resume, neuer Session-Start
      - _current_session_id gesetzt → --resume <id> wird übergeben
      - Nach erfolgreichem Call: Session-ID aus JSON-Output extrahieren (wenn konfiguriert)

    files: optionale Liste lokaler Dateipfade die an den Agenten übergeben werden.
      - cli_file_param gesetzt (z.B. --add-file): jede Datei als eigenes Flag-Paar
      - cli_file_param leer: Dateipfade werden dem Prompt vorangestellt
    """
    if not cfg.get('cli_command'):
        log.error('No cli_command configured. Set it via the web backend (Bridge konfigurieren).')
        return None

    cmd = [cfg['cli_command']]

    # Session fortsetzen wenn ID vorhanden (vom letzten erfolgreichen Call)
    session_param = cfg.get('cli_session_id_param', '')
    if session_param and _current_session_id:
        cmd += [session_param, _current_session_id]
        log.info(f'Continuing session: {_current_session_id}')
    else:
        log.info('Starting new session (no resume)')

    sp_param = cfg.get('cli_system_prompt_param', '')
    if sp_param and system_prompt:
        cmd += [sp_param, system_prompt]

    for arg in shlex.split(cfg.get('cli_extra_params', '')):
        cmd.append(arg)

    # Dateianhänge einbauen
    file_param = cfg.get('cli_file_param', '')
    if files:
        if file_param:
            for fp in files:
                cmd += [file_param, fp]
        else:
            log.warning('Files received but cli_file_param is not configured.')
            return None

    prompt_param = cfg.get('cli_prompt_param', '')
    if prompt_param:
        cmd += [prompt_param, prompt]
    else:
        cmd.append(prompt)

    env = os.environ.copy()
    env.update(cfg.get('cli_env', {}))

    cwd     = cfg.get('cli_working_dir') or None
    timeout = cfg.get('cli_timeout', 600)

    log.info(f'Calling CLI agent: {cmd[0]} (timeout={timeout}s, cwd={cwd})')
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            cwd=cwd,
            timeout=timeout,
        )
        if result.returncode != 0:
            log.error(f'CLI exited {result.returncode}')
            log.error(f'CMD:    {" ".join(cmd)}')
            log.error(f'STDERR: {result.stderr[:500]}')
            log.error(f'STDOUT: {result.stdout[:500]}')
            return None

        raw = result.stdout.strip()
        if not raw:
            log.error('CLI returned empty output')
            return None

        # JSON-Output parsen wenn konfiguriert (z.B. claude --output-format json)
        session_id_field = cfg.get('cli_session_id_output_field', '')
        if session_id_field:
            try:
                data = json.loads(raw)
                # Session-ID für nächsten Call speichern
                new_sid = data.get(session_id_field, '')
                if new_sid:
                    store_session_id(new_sid)
                # Antworttext aus "result"-Feld (Claude CLI) oder konfigurierbarem Feld
                answer_field = cfg.get('cli_answer_output_field', 'result')
                answer = data.get(answer_field, '').strip()
                if not answer:
                    log.error(f'JSON output has no "{answer_field}" field: {raw[:200]}')
                    return None
            except json.JSONDecodeError:
                log.error(f'Expected JSON output but got: {raw[:200]}')
                return None
        else:
            answer = raw

        log.info(f'CLI answered ({len(answer)} chars)')
        return answer

    except subprocess.TimeoutExpired:
        log.error(f'CLI timed out after {timeout}s')
        return None
    except Exception as e:
        log.error(f'CLI call failed: {e}')
        return None


# ─────────────────────────────────────────────
# Job processing
# ─────────────────────────────────────────────
def process_wakeup(cfg):
    """Wird aufgerufen wenn MQTT Wakeup-Message eintrifft"""
    log.info('Wakeup received — fetching job...')

    job = get_job(cfg)
    if not job:
        return

    prompt        = job.get('prompt', '')
    job_id        = job.get('job_id')
    system_prompt = job.get('system_prompt', '')
    reset         = job.get('reset_history', True)

    if not prompt or not job_id:
        log.warning('Job has no prompt or id, skipping.')
        return

    if reset:
        reset_session()
    log.info(f'Processing job #{job_id}: "{prompt[:60]}..."')

    answer = call_agent_cli(cfg, prompt, system_prompt)

    if answer:
        put_answer(cfg, job_id, answer)
    else:
        lang = cfg.get('lang', 'DE')
        err_msg = (
            'Es ist leider ein Fehler aufgetreten. Bitte versuche es erneut.'
            if lang == 'DE' else
            'An error occurred. Please try again.'
        )
        put_answer(cfg, job_id, err_msg)


# ─────────────────────────────────────────────
# MQTT
# ─────────────────────────────────────────────
def on_connect(client, userdata, flags, rc):
    cfg = userdata
    if rc == 0:
        topic = f"ac/{cfg['token_a']}"
        client.subscribe(topic, qos=1)
        log.info(f'Connected to MQTT broker, subscribed to: {topic}')
    else:
        log.error(f'MQTT connect failed, rc={rc}')


def on_message(client, userdata, msg):
    cfg = userdata
    raw = msg.payload.decode().strip()
    try:
        payload = json.loads(raw)
        action = payload.get('action', '')
    except Exception:
        # Plain-string payload (z.B. "ping" oder "wakeup")
        payload = {}
        action = raw

    log.info(f'MQTT message received: raw={raw!r} action={action}')

    if action == 'wakeup':
        process_wakeup(cfg)
    elif action == 'ping':
        send_pong(cfg)
    elif action == 'update-config':
        apply_config_update(cfg)
    else:
        log.warning(f'Unknown MQTT action: {action}')


def on_disconnect(client, userdata, rc):
    if rc != 0:
        log.warning(f'Unexpected MQTT disconnect (rc={rc}), reconnecting...')


# ─────────────────────────────────────────────
# Telegram long-polling
# ─────────────────────────────────────────────
def telegram_download_file(token, file_id, dest_dir):
    """Lädt eine Telegram-Datei herunter, gibt den lokalen Pfad zurück oder None."""
    try:
        r = requests.get(
            f'https://api.telegram.org/bot{token}/getFile',
            params={'file_id': file_id},
            timeout=10,
        )
        data = r.json()
        if not data.get('ok'):
            log.error(f'Telegram getFile failed: {data}')
            return None

        remote_path = data['result']['file_path']
        ext         = Path(remote_path).suffix

        r2   = requests.get(
            f'https://api.telegram.org/file/bot{token}/{remote_path}',
            timeout=60,
        )
        dest = Path(dest_dir) / f'tg_{file_id}{ext}'
        dest.write_bytes(r2.content)
        log.info(f'Telegram file downloaded: {dest} ({len(r2.content)} bytes)')
        return str(dest)
    except Exception as e:
        log.error(f'Telegram file download failed: {e}')
        return None


def telegram_send(token, chat_id, text):
    """Sendet eine Nachricht via Telegram Bot API."""
    url = f'https://api.telegram.org/bot{token}/sendMessage'
    try:
        requests.post(
            url,
            json={'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown'},
            timeout=10,
        )
    except Exception as e:
        log.error(f'Telegram send failed: {e}')


def telegram_send_typing(token, chat_id):
    """Sendet 'typing…' Indicator."""
    url = f'https://api.telegram.org/bot{token}/sendChatAction'
    try:
        requests.post(url, json={'chat_id': chat_id, 'action': 'typing'}, timeout=5)
    except Exception:
        pass


def telegram_poll_loop(cfg):
    """Lauscht per long-poll auf eingehende Telegram-Nachrichten.

    Nachrichten von telegram_chat_id werden an die KI weitergeleitet;
    die Antwort geht direkt per Telegram zurück.
    Läuft als Daemon-Thread parallel zum MQTT-Loop.
    """
    offset     = 0
    prev_token = ''

    while True:
        token   = cfg.get('telegram_bot_token', '')
        chat_id = str(cfg.get('telegram_chat_id', ''))

        if not token or not chat_id:
            time.sleep(30)
            continue

        # Offset zurücksetzen wenn sich der Token geändert hat
        if token != prev_token:
            offset     = 0
            prev_token = token
            log.info('Telegram polling started.')

        url    = f'https://api.telegram.org/bot{token}/getUpdates'
        params = {
            'offset':          offset,
            'timeout':         30,
            'allowed_updates': json.dumps(['message']),
        }

        try:
            r    = requests.get(url, params=params, timeout=35)
            data = r.json()
        except requests.exceptions.Timeout:
            continue
        except Exception as e:
            log.warning(f'Telegram poll error: {e}, retrying in 10s...')
            time.sleep(10)
            continue

        if not data.get('ok'):
            log.warning(f'Telegram getUpdates failed: {data}')
            time.sleep(10)
            continue

        for update in data.get('result', []):
            offset      = update['update_id'] + 1
            msg         = update.get('message', {})
            sender_id   = str(msg.get('chat', {}).get('id', ''))
            sender_name = msg.get('chat', {}).get('first_name', sender_id)
            text        = msg.get('text', '').strip()

            if sender_id != chat_id:
                log.debug(f'Telegram: message from unknown chat {sender_id}, ignored.')
                continue

            # Text aus message oder caption (bei Dateianhängen)
            text = (msg.get('text') or msg.get('caption') or '').strip()

            # Dateianhang erkennen und herunterladen
            attachment_info = None
            if   'photo'    in msg: attachment_info = ('photo',    msg['photo'][-1])
            elif 'document' in msg: attachment_info = ('document', msg['document'])
            elif 'audio'    in msg: attachment_info = ('audio',    msg['audio'])
            elif 'video'    in msg: attachment_info = ('video',    msg['video'])
            elif 'voice'    in msg: attachment_info = ('voice',    msg['voice'])

            downloaded_files = []
            if attachment_info:
                kind, attachment = attachment_info
                file_id  = attachment.get('file_id', '')
                base_dir = cfg.get('cli_working_dir') or str(Path(__file__).parent)
                dest_dir = str(Path(base_dir) / 'session_files')
                Path(dest_dir).mkdir(parents=True, exist_ok=True)
                log.info(f'Telegram attachment: {kind} (file_id={file_id})')
                local_path = telegram_download_file(token, file_id, dest_dir)
                if local_path:
                    downloaded_files.append(local_path)

            if not text and not downloaded_files:
                continue

            log.info(f'Telegram message from {sender_name} ({sender_id}): {text!r}'
                     + (f' + {len(downloaded_files)} file(s)' if downloaded_files else ''))

            if not cfg.get('cli_command'):
                log.warning('Telegram: no cli_command configured, cannot process message.')
                telegram_send(token, chat_id, '⚠️ Bridge nicht konfiguriert. Bitte CLI-Konfiguration im Backend setzen.')
                continue

            system_prompt = cfg.get('telegram_system_prompt', '')
            telegram_send_typing(token, chat_id)

            answer = call_agent_cli(cfg, text, system_prompt, files=downloaded_files or None)

            # Temp-Dateien aufräumen
            for fp in downloaded_files:
                try:
                    Path(fp).unlink(missing_ok=True)
                except Exception:
                    pass

            if answer:
                log.info(f'Telegram answer ({len(answer)} chars) sent to {sender_id}')
                telegram_send(token, chat_id, answer)
            else:
                lang = cfg.get('lang', 'DE')
                if downloaded_files and not cfg.get('cli_file_param'):
                    err = ('Dateianhänge werden erst nach Konfiguration des File-Parameters im Backend unterstützt.'
                           if lang == 'DE' else
                           'File attachments require a configured file parameter in the backend.')
                else:
                    err = ('Es ist leider ein Fehler aufgetreten. Bitte versuche es erneut.'
                           if lang == 'DE' else
                           'An error occurred. Please try again.')
                telegram_send(token, chat_id, err)


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────
def main():
    cfg = load_config()

    required = ['token_a', 'token_b', 'server_url',
                'mqtt_host', 'mqtt_port', 'mqtt_user', 'mqtt_password']
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        log.error(f'Missing config keys: {missing}')
        sys.exit(1)

    client = mqtt.Client(
        client_id=f"ac-bridge-{cfg['token_a'][:8]}",
        userdata=cfg,
    )
    client.username_pw_set(cfg['mqtt_user'], cfg['mqtt_password'])
    client.on_connect    = on_connect
    client.on_message    = on_message
    client.on_disconnect = on_disconnect

    # TLS falls konfiguriert
    if cfg.get('mqtt_tls', False):
        client.tls_set()

    def shutdown(sig, frame):
        log.info('Shutting down bridge...')
        client.disconnect()
        sys.exit(0)

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    log.info(f"AC Bridge starting — server: {cfg['server_url']}")
    log.info(f"MQTT: {cfg['mqtt_host']}:{cfg['mqtt_port']}, topic: ac/{cfg['token_a']}")

    tg_thread = threading.Thread(
        target=telegram_poll_loop, args=(cfg,), daemon=True, name='tg-poll'
    )
    tg_thread.start()
    log.info('Telegram polling thread started.')

    while True:
        try:
            client.connect(cfg['mqtt_host'], int(cfg['mqtt_port']), keepalive=60)
            client.loop_forever()
        except Exception as e:
            log.error(f'MQTT connection error: {e}, retrying in 30s...')
            time.sleep(30)


if __name__ == '__main__':
    main()