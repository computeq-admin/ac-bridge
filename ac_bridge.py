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

# update-test 2026-05-31 #2: Self-Update-Flow erneut prüfen (mit Auto-Refresh der App-Anzeige)

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
LOG_FILE = Path(__file__).resolve().parent / 'ac_bridge.log'
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE),
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

# ─────────────────────────────────────────────
# Self-Update (öffentliches Repo, HTTPS read-only)
# ─────────────────────────────────────────────
REPO_DIR         = Path(__file__).resolve().parent
GITHUB_FETCH_URL = 'https://github.com/computeq-admin/ac-bridge.git'
GIT_BRANCH       = 'main'
UPDATE_CHECK_TTL = 3600  # Sekunden: Remote-Versionscheck höchstens stündlich

# Gecachter Update-Status (wird im Heartbeat an den Server gemeldet)
_update_status = {'commit': '', 'available': False, 'ts': 0.0}

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
        executable = Path(shlex.split(cli_command)[0]).name
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


def _git_local_commit():
    """Vollständiger lokaler Commit-Hash oder '' bei Fehler."""
    try:
        out = subprocess.run(
            ['git', '-C', str(REPO_DIR), 'rev-parse', 'HEAD'],
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() if out.returncode == 0 else ''
    except Exception as e:
        log.error(f'git rev-parse failed: {e}')
        return ''


def _git_remote_commit():
    """Vollständiger Remote-Commit-Hash von origin/main via HTTPS oder '' bei Fehler."""
    try:
        out = subprocess.run(
            ['git', 'ls-remote', GITHUB_FETCH_URL, f'refs/heads/{GIT_BRANCH}'],
            capture_output=True, text=True, timeout=20,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.split()[0].strip()
    except Exception as e:
        log.error(f'git ls-remote failed: {e}')
    return ''


def refresh_update_status(force=False):
    """Vergleicht lokalen mit Remote-Commit, max. einmal pro UPDATE_CHECK_TTL.

    Aktualisiert den gecachten _update_status (commit kurz + available). Vergleich
    erfolgt über die vollen Hashes, gemeldet/angezeigt wird der gekürzte Commit."""
    now = time.time()
    if not force and (now - _update_status['ts'] < UPDATE_CHECK_TTL):
        return
    local  = _git_local_commit()
    remote = _git_remote_commit()
    if local:
        _update_status['commit'] = local[:7]
        # Nur als veraltet melden, wenn der Remote-Check erfolgreich war.
        _update_status['available'] = bool(remote and local != remote)
        _update_status['ts'] = now
        log.info(f'Update check: local={local[:7]} remote={remote[:7] or "?"} '
                 f'available={_update_status["available"]}')


def perform_self_update(cfg):
    """git fetch + hard reset auf origin/main (HTTPS), pip install, Service-Neustart.

    Wird via MQTT action=update ausgelöst. config.json ist .gitignored, daher ist
    `reset --hard` gefahrlos und vermeidet Merge-Konflikte."""
    log.info('Self-update requested')
    try:
        subprocess.run(
            ['git', '-C', str(REPO_DIR), 'fetch', GITHUB_FETCH_URL, GIT_BRANCH],
            capture_output=True, text=True, timeout=60, check=True,
        )
        subprocess.run(
            ['git', '-C', str(REPO_DIR), 'reset', '--hard', 'FETCH_HEAD'],
            capture_output=True, text=True, timeout=30, check=True,
        )
        log.info(f'Code updated to {_git_local_commit()[:7]}')
    except Exception as e:
        log.error(f'Self-update git step failed: {e}')
        return False

    # Abhängigkeiten aktualisieren (best effort)
    venv_pip = REPO_DIR / 'venv' / 'bin' / 'pip'
    req      = REPO_DIR / 'requirements.txt'
    if venv_pip.exists() and req.exists():
        try:
            subprocess.run(
                [str(venv_pip), 'install', '-r', str(req)],
                capture_output=True, text=True, timeout=180,
            )
        except Exception as e:
            log.error(f'pip install after update failed: {e}')

    # Neustart über systemd lädt den neuen Code (gleiches Muster wie update-config)
    _update_status['ts'] = 0.0  # erzwingt frischen Check nach Neustart
    service_name = cfg.get('service_name', '')
    if service_name:
        log.info(f'Restarting service after update: {service_name}')
        try:
            subprocess.run(['systemctl', '--user', 'restart', service_name], timeout=10)
        except Exception as e:
            log.error(f'Restart after update failed: {e}')
    else:
        log.warning('No service_name set; exiting so systemd restarts the process')
        os._exit(0)
    return True


def send_bridge_log(cfg, max_lines=800):
    """Liest die letzten max_lines Zeilen des Logs und schickt sie an den Server.

    Wird via MQTT action=send-log ausgelöst (Support-Anfrage aus der App)."""
    try:
        with open(LOG_FILE, 'r', errors='replace') as f:
            lines = f.readlines()
        tail = ''.join(lines[-max_lines:])
    except Exception as e:
        log.error(f'send_bridge_log: reading log failed: {e}')
        tail = f'(Logdatei konnte nicht gelesen werden: {e})'

    try:
        r = requests.post(
            cfg['server_url'] + '/put_bridge_log.php',
            json={'token_b': cfg['token_b'], 'log': tail},
            timeout=15,
        )
        data = r.json()
        if 'token_b_new' in data:
            cfg['token_b'] = data['token_b_new']
            save_config(cfg)
        log.info(f'Bridge log sent ({len(tail)} bytes)')
    except Exception as e:
        log.error(f'send_bridge_log: post failed: {e}')


def send_pong(cfg):
    """Antwortet auf Server-Ping, rotiert Token-B"""
    refresh_update_status()
    try:
        r = requests.post(
            cfg['server_url'] + '/ping.php',
            json={
                'token_b':          cfg['token_b'],
                'bridge_commit':    _update_status['commit'],
                'update_available': _update_status['available'],
            },
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


def put_answer(cfg, job_id, answer, session_id=None):
    """Schreibt Antwort zurück, rotiert Token-B.

    session_id: die (neue oder fortgesetzte) Claude-Session-ID aus diesem Call, falls
    vom Agenten geliefert. Wird nur mitgeschickt wenn vorhanden — put_answer.php lässt
    die gespeicherte session_id sonst unangetastet.
    """
    payload = {
        'token_b': cfg['token_b'],
        'job_id':  job_id,
        'answer':  answer,
    }
    if session_id:
        payload['session_id'] = session_id
    try:
        r = requests.post(
            cfg['server_url'] + '/put_answer.php',
            json=payload,
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
# JSON helpers
# ─────────────────────────────────────────────
def json_get(data, path):
    """Liest einen Wert aus verschachteltem JSON per Dot-Notation.
    Beispiel: json_get(data, 'result.meta.agentMeta.sessionId')
    Gibt None zurück wenn der Pfad nicht existiert.
    """
    val = data
    for part in path.split('.'):
        if isinstance(val, dict):
            val = val.get(part)
        elif isinstance(val, list) and part.isdigit():
            val = val[int(part)]
        else:
            return None
    return val


# ─────────────────────────────────────────────
# Session management
# ─────────────────────────────────────────────
_current_session_id  = None
_openclaw_agent_id   = None   # cached after first successful 'openclaw agents list'


def reset_session():
    global _current_session_id
    _current_session_id = None
    log.info('Session reset — next call starts a new session.')


def store_session_id(session_id):
    global _current_session_id
    _current_session_id = session_id
    log.info(f'Session ID stored: {session_id}')


# ─────────────────────────────────────────────
# Openclaw agent auto-discovery
# ─────────────────────────────────────────────
def discover_openclaw_agent_id(cli_binary, cwd):
    """Run 'openclaw agents list --json' once and cache the first agent's ID."""
    global _openclaw_agent_id
    if _openclaw_agent_id:
        return _openclaw_agent_id
    log.info('Auto-discovering openclaw agent ID ...')
    try:
        result = subprocess.run(
            [cli_binary, 'agents', 'list', '--json'],
            capture_output=True, text=True, timeout=15, cwd=cwd
        )
        output = result.stdout.strip()
        if result.returncode == 0 and output:
            data = json.loads(output)
            agents = data if isinstance(data, list) else data.get('agents', [])
            if agents:
                agent = agents[0]
                agent_id = (agent.get('id') or agent.get('agentId')
                            or agent.get('agent_id') or agent.get('_id'))
                if agent_id:
                    _openclaw_agent_id = str(agent_id)
                    log.info(f'Openclaw agent auto-discovered: {_openclaw_agent_id} ({agent.get("name", "")})')
                    return _openclaw_agent_id
        log.warning(f'openclaw agents list returned no usable ID (rc={result.returncode}): {output[:200]}')
    except Exception as e:
        log.error(f'Failed to auto-discover openclaw agent ID: {e}')
    return None


# ─────────────────────────────────────────────
# Agent call (CLI)
# ─────────────────────────────────────────────
def call_agent_cli(cfg, prompt, system_prompt='', files=None, session_id_override=None):
    """Ruft den KI-Agenten als lokalen CLI-Prozess auf.

    Session-Handling:
      - session_id_override gesetzt (App will ein bestimmtes altes Gespräch fortsetzen):
        hat Vorrang vor der RAM-gehaltenen _current_session_id für diesen einen Call.
      - Sonst: kein _current_session_id → kein --resume, neuer Session-Start;
               _current_session_id gesetzt → --resume <id> wird übergeben (Legacy-Verhalten
               für Alexa/Siri/ältere App-Versionen, die keine session_id mitschicken).
      - Nach erfolgreichem Call: Session-ID aus JSON-Output extrahieren (wenn konfiguriert),
        als neue _current_session_id übernehmen UND zurückgegeben (für put_answer).

    files: optionale Liste lokaler Dateipfade die an den Agenten übergeben werden.
      - cli_file_param gesetzt (z.B. --add-file): jede Datei als eigenes Flag-Paar
      - cli_file_param leer: Dateipfade werden dem Prompt vorangestellt

    Rückgabe: (answer, session_id) — answer ist None bei Fehler; session_id ist die aus
    dem Output extrahierte ID oder None (kein Feld konfiguriert / nicht vorhanden).
    """
    if not cfg.get('cli_command'):
        log.error('No cli_command configured. Set it via the web backend (Bridge konfigurieren).')
        return None, None

    cmd = shlex.split(os.path.expanduser(cfg['cli_command']))

    # Session-Handling
    session_param    = cfg.get('cli_session_id_param', '')
    session_id_field = cfg.get('cli_session_id_output_field', '')
    resume_id        = session_id_override or _current_session_id

    if session_param:
        if resume_id:
            origin = ' (App-Fortsetzen)' if session_id_override else ''
            cmd += [session_param, resume_id]
            log.info(f'Continuing session: {resume_id}{origin}')
        elif not session_id_field:
            # Input-Modus (z.B. openclaw): Session-ID wird immer übergeben,
            # kommt nicht aus dem Output → neue UUID generieren
            import uuid as _uuid
            new_id = str(_uuid.uuid4())
            store_session_id(new_id)
            cmd += [session_param, new_id]
            log.info(f'New session (generated ID): {new_id}')
        else:
            log.info('Starting new session (no resume)')

    # Openclaw requires --agent <id> when starting a new session so it knows
    # which configured agent to invoke. Auto-discover via 'openclaw agents list'.
    if 'openclaw' in os.path.basename(cmd[0]) and not resume_id:
        _cwd = os.path.expanduser(cfg.get('cli_working_dir') or '') or None
        agent_id = discover_openclaw_agent_id(cmd[0], _cwd)
        if agent_id:
            cmd += ['--agent', agent_id]
        else:
            log.warning('Could not auto-discover openclaw agent ID — add "--agent <id>" to cli_extra_params manually.')

    sp_param = cfg.get('cli_system_prompt_param', '')
    if system_prompt:
        if sp_param:
            log.info(f'Passing system prompt via param "{sp_param}" ({len(system_prompt)} chars): "{system_prompt[:120]}"')
            cmd += [sp_param, system_prompt]
        else:
            log.info(f'No sp_param configured — prepending system prompt to prompt ({len(system_prompt)} chars): "{system_prompt[:120]}"')
            prompt = f'{system_prompt}\n\n{prompt}' if prompt else system_prompt
    else:
        log.info('No system prompt passed to agent CLI.')

    for arg in shlex.split(cfg.get('cli_extra_params', '')):
        cmd.append(os.path.expanduser(arg))

    # Dateianhänge einbauen
    file_param = cfg.get('cli_file_param', '')
    if files:
        if file_param:
            for fp in files:
                cmd += [file_param, fp]
        else:
            # Kein CLI-Flag konfiguriert: Dateipfad im Prompt nennen.
            # Claude Code CLI hat den Read-Tool und liest die Datei selbst.
            file_note = '\n'.join(
                f'The user sent the following file, please read and process it: {Path(fp).resolve()}'
                for fp in files
            )
            prompt = f'{file_note}\n\n{prompt}' if prompt else file_note

    prompt_param = cfg.get('cli_prompt_param', '')
    if prompt_param:
        cmd += [prompt_param, prompt]
    else:
        cmd.append(prompt)

    env = os.environ.copy()
    env.update(cfg.get('cli_env', {}))

    cwd     = os.path.expanduser(cfg['cli_working_dir']) if cfg.get('cli_working_dir') else None
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
            return None, None

        raw = result.stdout.strip()
        if not raw:
            log.error('CLI returned empty output')
            return None, None

        # JSON-Output parsen wenn session_id_field gesetzt ODER answer_field Dot-Notation enthält
        answer_field     = cfg.get('cli_answer_output_field', 'result')
        parse_json       = bool(session_id_field) or '.' in answer_field
        extracted_sid    = None
        if parse_json:
            try:
                data = json.loads(raw)
                if session_id_field:
                    new_sid = json_get(data, session_id_field) or ''
                    if new_sid:
                        extracted_sid = str(new_sid)
                        store_session_id(extracted_sid)
                answer = (json_get(data, answer_field) or '').strip()
                if not answer:
                    log.error(f'JSON output has no "{answer_field}" field: {raw[:200]}')
                    return None, None
            except json.JSONDecodeError:
                log.error(f'Expected JSON output but got: {raw[:200]}')
                return None, None
        else:
            answer = raw

        log.info(f'CLI answered ({len(answer)} chars)')
        return answer, extracted_sid

    except subprocess.TimeoutExpired:
        log.error(f'CLI timed out after {timeout}s')
        return None, None
    except Exception as e:
        log.error(f'CLI call failed: {e}')
        return None, None


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
    image_data_b64 = job.get('image_data', '')
    # App-gesteuertes Fortsetzen eines bestimmten (u.U. alten) Gesprächs: hat Vorrang
    # vor der RAM-gehaltenen Session. Leer bei Legacy-Jobs (Alexa/Siri/alte App) oder
    # wenn reset_history bereits True ist — dann verhält sich alles wie bisher.
    job_session_id = job.get('session_id') or None

    if not prompt or not job_id:
        log.warning('Job has no prompt or id, skipping.')
        return

    if reset:
        reset_session()
    log.info(f'Processing job #{job_id}: "{prompt[:60]}..."')
    if system_prompt:
        log.info(f'System prompt received ({len(system_prompt)} chars): "{system_prompt[:120]}"')
    else:
        log.info('System prompt: (none received from server)')

    # Bild dekodieren und als Temp-Datei ablegen
    downloaded_files = []
    if image_data_b64:
        import base64 as _base64
        try:
            image_bytes = _base64.b64decode(image_data_b64)
            # Always under the bridge's own directory, independent of cli_working_dir
            # (which is where the AGENT process runs, e.g. the user's home dir for
            # OpenClaw's "~" default — not where the bridge should stash its files).
            dest_dir = REPO_DIR / 'session_files'
            dest_dir.mkdir(parents=True, exist_ok=True)
            # Timestamped so repeated uploads for the same job (single ac_jobs row per
            # device) don't overwrite each other; files are kept, not cleaned up below.
            timestamp = time.strftime('%Y%m%d_%H%M%S')
            dest = dest_dir / f'ios_img_{job_id}_{timestamp}.jpg'
            dest.write_bytes(image_bytes)
            downloaded_files.append(str(dest))
            log.info(f'Image decoded for job #{job_id}: {dest} ({len(image_bytes)} bytes)')
        except Exception as e:
            log.error(f'Image decode failed for job #{job_id}: {e}')

    answer, new_session_id = call_agent_cli(
        cfg, prompt, system_prompt, files=downloaded_files or None,
        session_id_override=job_session_id,
    )

    if answer:
        put_answer(cfg, job_id, answer, session_id=new_session_id)
    else:
        lang = cfg.get('lang', 'DE')
        err_msg = (
            'Es ist leider ein Fehler aufgetreten. Bitte versuche es erneut.'
            if lang == 'DE' else
            'An error occurred. Please try again.'
        )
        put_answer(cfg, job_id, err_msg)


# ─────────────────────────────────────────────
# Conversation History (Verlauf-Tab)
#
# Statt den Gesprächsverlauf clientseitig zu spiegeln, liest die Bridge ihn bei
# Bedarf direkt aus den Session-Dateien des jeweiligen Agenten:
#   - Claude:   ~/.claude/projects/<encoded-cwd>/<session-id>.jsonl
#   - Openclaw: ~/.openclaw/agents/<agent>/sessions/<session-id>.trajectory.jsonl
# 'encoded-cwd' bzw. der Agent-Ordner werden aus derselben Konfiguration/
# Discovery abgeleitet, die call_agent_cli() ohnehin schon verwendet — damit
# passt die History garantiert zum tatsächlich aufgerufenen Agenten/Projekt.
# ─────────────────────────────────────────────
HISTORY_UPLOAD_MAX_CHARS = 500_000  # Schutz gegen Riesen-Payloads (analog send_bridge_log)


def _iso_from_mtime(path):
    import datetime
    return datetime.datetime.utcfromtimestamp(path.stat().st_mtime).isoformat() + 'Z'


def _effective_cwd(cfg):
    """Das Arbeitsverzeichnis, in dem der Agent tatsächlich läuft — exakt wie in
    call_agent_cli() ermittelt (dort mit cwd=None an subprocess.run übergeben,
    was das eigene Arbeitsverzeichnis der Bridge bedeutet)."""
    cwd = os.path.expanduser(cfg['cli_working_dir']) if cfg.get('cli_working_dir') else None
    return cwd or os.getcwd()


def _cli_binary(cfg):
    cmd = shlex.split(os.path.expanduser(cfg.get('cli_command', '') or ''))
    return cmd[0] if cmd else ''


# --- Claude ---

def _claude_project_dir(cfg):
    encoded = _effective_cwd(cfg).replace('/', '-')
    return Path.home() / '.claude' / 'projects' / encoded


def _claude_extract_text(message):
    if not isinstance(message, dict):
        return ''
    content = message.get('content')
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [b.get('text', '') for b in content if isinstance(b, dict) and b.get('type') == 'text']
        return '\n'.join(p for p in parts if p).strip()
    return ''


def _claude_session_files(cfg):
    d = _claude_project_dir(cfg)
    if not d.is_dir():
        return []
    return sorted(d.glob('*.jsonl'), key=lambda p: p.stat().st_mtime, reverse=True)


def _read_claude_summary(path):
    title = None
    last_ts = None
    msg_count = 0
    first_user_text = None
    try:
        with open(path, 'r', errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get('isSidechain'):
                    continue  # Tool-/Sub-Agent-interne Nebenläufe, kein sichtbarer Chat-Turn
                t = o.get('type')
                if t == 'ai-title' and o.get('aiTitle'):
                    title = o['aiTitle']
                elif t in ('user', 'assistant'):
                    text = _claude_extract_text(o.get('message') or {})
                    if not text:
                        continue
                    msg_count += 1
                    if o.get('timestamp'):
                        last_ts = o['timestamp']
                    if t == 'user' and first_user_text is None:
                        first_user_text = text
    except Exception as e:
        log.error(f'claude history summary failed for {path}: {e}')
        return None
    if msg_count == 0:
        return None
    return {
        'session_id':     path.stem,
        'title':          title or (first_user_text or 'Gespräch')[:60],
        'updated_at':     last_ts or _iso_from_mtime(path),
        'message_count':  msg_count,
    }


def _read_claude_detail(path):
    messages = []
    try:
        with open(path, 'r', errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get('isSidechain'):
                    continue
                t = o.get('type')
                if t not in ('user', 'assistant'):
                    continue
                text = _claude_extract_text(o.get('message') or {})
                if not text:
                    continue
                messages.append({
                    'role':      'user' if t == 'user' else 'agent',
                    'content':   text,
                    'timestamp': o.get('timestamp') or '',
                })
    except Exception as e:
        log.error(f'claude history detail failed for {path}: {e}')
        return None
    return messages


# --- Openclaw ---

def _openclaw_agents_root():
    return Path.home() / '.openclaw' / 'agents'


def _openclaw_peek_agent_id(path):
    """Liest nur die ersten Zeilen, um die 'session.started'-Zeile mit dem
    agentId zu finden, ohne die ganze (potenziell große) Datei zu parsen."""
    try:
        with open(path, 'r', errors='replace') as f:
            for i, line in enumerate(f):
                if i >= 5:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get('type') == 'session.started':
                    return (o.get('data') or {}).get('agentId')
    except Exception:
        return None
    return None


def _openclaw_session_files(cfg, cli_binary):
    """Alle Trajectory-Dateien, deren agentId zum per Discovery ermittelten
    Agenten passt. Iteriert bewusst über ALLE Agent-Unterordner statt einen
    bestimmten Ordnernamen anzunehmen — der Ordnername entspricht zwar in der
    Praxis der Agent-ID, ist das aber nicht garantiert dokumentiert. Schlägt
    die Discovery fehl (agent_id=None), werden alle Sessions ungefiltert
    zurückgegeben (lieber zu viel zeigen als gar keine Historie)."""
    root = _openclaw_agents_root()
    if not root.is_dir():
        return []
    cwd = _effective_cwd(cfg)
    agent_id = discover_openclaw_agent_id(cli_binary, cwd)
    matches = []
    for sessions_dir in root.glob('*/sessions'):
        for path in sessions_dir.glob('*.trajectory.jsonl'):
            if agent_id is not None:
                file_agent_id = _openclaw_peek_agent_id(path)
                if file_agent_id is not None and str(file_agent_id) != str(agent_id):
                    continue
            matches.append(path)
    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return matches


def _read_openclaw_summary(path):
    session_id = None
    last_ts = None
    msg_count = 0
    first_prompt = None
    try:
        with open(path, 'r', errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if session_id is None:
                    session_id = o.get('sessionId')
                if o.get('ts'):
                    last_ts = o['ts']
                t = o.get('type')
                if t == 'prompt.submitted':
                    text = ((o.get('data') or {}).get('prompt') or '').strip()
                    if text:
                        msg_count += 1
                        if first_prompt is None:
                            first_prompt = text
                elif t == 'model.completed':
                    texts = (o.get('data') or {}).get('assistantTexts') or []
                    if any(isinstance(x, str) and x.strip() for x in texts):
                        msg_count += 1
    except Exception as e:
        log.error(f'openclaw history summary failed for {path}: {e}')
        return None
    if msg_count == 0:
        return None
    return {
        'session_id':    session_id or path.name.split('.trajectory.jsonl')[0],
        'title':         (first_prompt or 'Gespräch')[:60],
        'updated_at':    last_ts or _iso_from_mtime(path),
        'message_count': msg_count,
    }


def _read_openclaw_detail(path):
    messages = []
    try:
        with open(path, 'r', errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                t = o.get('type')
                ts = o.get('ts', '')
                if t == 'prompt.submitted':
                    text = ((o.get('data') or {}).get('prompt') or '').strip()
                    if text:
                        messages.append({'role': 'user', 'content': text, 'timestamp': ts})
                elif t == 'model.completed':
                    texts = (o.get('data') or {}).get('assistantTexts') or []
                    joined = '\n\n'.join(x.strip() for x in texts if isinstance(x, str) and x.strip())
                    if joined:
                        messages.append({'role': 'agent', 'content': joined, 'timestamp': ts})
    except Exception as e:
        log.error(f'openclaw history detail failed for {path}: {e}')
        return None
    return messages


# --- Dispatch (Backend anhand cli_command wählen) ---

def _history_session_files(cfg):
    binary = _cli_binary(cfg)
    if not binary:
        return [], None
    if 'openclaw' in os.path.basename(binary):
        return _openclaw_session_files(cfg, binary), 'openclaw'
    return _claude_session_files(cfg), 'claude'


def build_history_list(cfg):
    files, backend = _history_session_files(cfg)
    if backend is None:
        return []
    reader = _read_openclaw_summary if backend == 'openclaw' else _read_claude_summary
    entries = []
    for path in files:
        entry = reader(path)
        if entry:
            entries.append(entry)
    return entries


def build_history_detail(cfg, session_id):
    files, backend = _history_session_files(cfg)
    if backend is None:
        return None
    reader = _read_openclaw_detail if backend == 'openclaw' else _read_claude_detail
    for path in files:
        # Claude-Dateiname == Session-ID exakt; Openclaw-Dateiname beginnt damit
        # (Suffix .trajectory.jsonl).
        if path.stem == session_id or path.name.startswith(session_id):
            return reader(path)
    return None


def send_history(cfg):
    """Liest alle Gesprächs-Sessions des konfigurierten Agenten (Zusammenfassung:
    Titel, Datum, Nachrichtenzahl) und schickt sie an den Server.
    Wird via MQTT action=send-history ausgelöst (App öffnet den Verlauf-Tab)."""
    try:
        entries = build_history_list(cfg)
    except Exception as e:
        log.error(f'send_history: build_history_list failed: {e}')
        entries = []

    while entries and len(json.dumps(entries)) > HISTORY_UPLOAD_MAX_CHARS:
        entries.pop()  # älteste (am Ende, da neuste zuerst sortiert) zuerst raus

    try:
        r = requests.post(
            cfg['server_url'] + '/put_bridge_history.php',
            json={'token_b': cfg['token_b'], 'kind': 'list', 'history': entries},
            timeout=20,
        )
        data = r.json()
        if 'token_b_new' in data:
            cfg['token_b'] = data['token_b_new']
            save_config(cfg)
        if data.get('status') == 'ok':
            log.info(f'History list sent ({len(entries)} Gespräche)')
        else:
            log.error(f'send_history: server rejected upload (HTTP {r.status_code}): {data}')
    except Exception as e:
        log.error(f'send_history: post failed: {e}')


def send_history_detail(cfg, session_id):
    """Liest das volle Transkript einer Session und schickt es an den Server.
    Wird via MQTT action=send-history-detail ausgelöst (Nutzer tippt ein
    Gespräch im Verlauf-Tab an)."""
    if not session_id:
        log.warning('send_history_detail: no session_id in payload')
        return
    try:
        messages = build_history_detail(cfg, session_id)
    except Exception as e:
        log.error(f'send_history_detail: build_history_detail failed: {e}')
        messages = None
    if messages is None:
        messages = []

    while messages and len(json.dumps(messages)) > HISTORY_UPLOAD_MAX_CHARS:
        messages.pop(0)  # älteste Nachrichten zuerst verwerfen, Gesprächsende behalten

    try:
        r = requests.post(
            cfg['server_url'] + '/put_bridge_history.php',
            json={'token_b': cfg['token_b'], 'kind': 'detail', 'session_id': session_id, 'messages': messages},
            timeout=20,
        )
        data = r.json()
        if 'token_b_new' in data:
            cfg['token_b'] = data['token_b_new']
            save_config(cfg)
        if data.get('status') == 'ok':
            log.info(f'History detail sent for session {session_id} ({len(messages)} Nachrichten)')
        else:
            log.error(f'send_history_detail: server rejected upload (HTTP {r.status_code}): {data}')
    except Exception as e:
        log.error(f'send_history_detail: post failed: {e}')


# ─────────────────────────────────────────────
# MQTT
# ─────────────────────────────────────────────
def on_connect(client, userdata, flags, rc):
    cfg = userdata
    if rc == 0:
        topic = f"ac/{cfg['token_a']}"
        client.subscribe(topic, qos=1)
        log.info(f'Connected to MQTT broker, subscribed to: {topic}')
        threading.Thread(target=send_pong, args=(cfg,), daemon=True).start()
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
    elif action == 'update':
        perform_self_update(cfg)
    elif action == 'send-log':
        send_bridge_log(cfg)
    elif action == 'send-history':
        send_history(cfg)
    elif action == 'send-history-detail':
        send_history_detail(cfg, payload.get('session_id', ''))
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


TG_MAX_LEN = 4096


def _tg_post(token, chat_id, text, parse_mode=None):
    """Einzelner API-Call; gibt True bei Erfolg zurück."""
    url  = f'https://api.telegram.org/bot{token}/sendMessage'
    body = {'chat_id': chat_id, 'text': text}
    if parse_mode:
        body['parse_mode'] = parse_mode
    try:
        r    = requests.post(url, json=body, timeout=10)
        data = r.json()
        if not data.get('ok'):
            log.warning(f'Telegram sendMessage failed ({parse_mode}): {data.get("description")}')
            return False
        return True
    except Exception as e:
        log.error(f'Telegram send failed: {e}')
        return False


def telegram_send(token, chat_id, text):
    """Sendet eine Nachricht via Telegram Bot API.

    Teilt lange Texte in Chunks auf (4096-Zeichen-Limit).
    Versucht zuerst Markdown-Formatierung; bei Parse-Fehler Fallback auf Plain Text.
    """
    chunks = [text[i:i + TG_MAX_LEN] for i in range(0, len(text), TG_MAX_LEN)]
    for chunk in chunks:
        if not _tg_post(token, chat_id, chunk, parse_mode='Markdown'):
            _tg_post(token, chat_id, chunk)


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

            # /new — neue Session starten
            if text.lower() in ('/new', '/new@' + token.split(':')[0]):
                reset_session()
                lang = cfg.get('lang', 'DE')
                confirm = ('Neue Session gestartet.' if lang == 'DE' else 'New session started.')
                telegram_send(token, chat_id, confirm)
                log.info(f'Telegram /new: session reset by {sender_name}')
                continue

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
                # Always under the bridge's own directory — see comment in process_wakeup.
                dest_dir = str(REPO_DIR / 'session_files')
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

            system_prompt = cfg.get('system_prompt_chat', '') or cfg.get('telegram_system_prompt', '')
            telegram_send_typing(token, chat_id)

            answer, _ = call_agent_cli(cfg, text, system_prompt, files=downloaded_files or None)

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
                err = ('Es ist leider ein Fehler aufgetreten. Bitte versuche es erneut.'
                       if lang == 'DE' else
                       'An error occurred. Please try again.')
                telegram_send(token, chat_id, err)


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────
def main():
    cfg = load_config()

    telegram_only = cfg.get('telegram_only', False)
    client = None

    if telegram_only:
        log.info('AC Bridge starting in Telegram-only mode (no MQTT)')
    else:
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

        if cfg.get('mqtt_tls', False):
            client.tls_set()

        log.info(f"AC Bridge starting — server: {cfg['server_url']}")
        log.info(f"MQTT: {cfg['mqtt_host']}:{cfg['mqtt_port']}, topic: ac/{cfg['token_a']}")

    def shutdown(sig, frame):
        log.info('Shutting down bridge...')
        if client:
            client.disconnect()
        sys.exit(0)

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    tg_thread = threading.Thread(
        target=telegram_poll_loop, args=(cfg,), daemon=True, name='tg-poll'
    )
    tg_thread.start()
    log.info('Telegram polling thread started.')

    if telegram_only:
        while True:
            time.sleep(60)
    else:
        while True:
            try:
                client.connect(cfg['mqtt_host'], int(cfg['mqtt_port']), keepalive=60)
                client.loop_forever()
            except Exception as e:
                log.error(f'MQTT connection error: {e}, retrying in 30s...')
                time.sleep(30)


if __name__ == '__main__':
    main()