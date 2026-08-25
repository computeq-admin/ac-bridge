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
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
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
CONFIG_FILE           = Path(__file__).parent / 'config.json'
TELEGRAM_CONFIG_FILE  = Path(__file__).parent / 'telegram-config.json'
HERMES_TITLE_CACHE_FILE = Path(__file__).parent / 'hermes_title_cache.json'
TELEGRAM_CONFIG_KEYS = ('telegram_chat_id', 'telegram_bot_token', 'telegram_system_prompt')

PROTECTED_CONFIG_KEYS = {
    'token_a', 'mqtt_host', 'mqtt_port', 'mqtt_user',
    'mqtt_password', 'mqtt_tls', 'server_url', 'token_b',
    'service_name',
}

ALLOWED_CLI_EXECUTABLES = {
    'claude', 'openclaw', 'hermes', 'copilot', 'gemini', 'aider', 'interpreter', 'goose',
}

# ─────────────────────────────────────────────
# Self-Update (öffentliches Repo, HTTPS read-only)
# ─────────────────────────────────────────────
REPO_DIR         = Path(__file__).resolve().parent
GITHUB_FETCH_URL = 'https://github.com/computeq-admin/ac-bridge.git'
GIT_BRANCH       = 'main'

def load_config():
    if not CONFIG_FILE.exists():
        log.error('config.json not found. Run setup first.')
        sys.exit(1)
    with open(CONFIG_FILE) as f:
        return json.load(f)

def save_config(cfg):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(cfg, f, indent=2)

def load_hermes_title_cache():
    """session_id -> abgeleiteter Titel. Vergangene Gespräche ändern sich nie
    wieder — einmal aufgelöste Platzhalter-Titel (siehe _hermes_history_list)
    müssen nicht bei jedem Verlauf-Tab-Öffnen erneut per Export ermittelt werden."""
    if not HERMES_TITLE_CACHE_FILE.exists():
        return {}
    try:
        with open(HERMES_TITLE_CACHE_FILE) as f:
            return json.load(f)
    except Exception as e:
        log.error(f'hermes_title_cache.json konnte nicht gelesen werden: {e}')
        return {}

def save_hermes_title_cache(cache):
    try:
        with open(HERMES_TITLE_CACHE_FILE, 'w') as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        log.error(f'hermes_title_cache.json konnte nicht geschrieben werden: {e}')

def apply_local_telegram_config(cfg):
    """Prüft bei jedem Bridge-Start, ob telegram-config.json im Bridge-Verzeichnis
    liegt, und übernimmt deren Felder (telegram_chat_id, telegram_bot_token,
    telegram_system_prompt) in config.json. Ermöglicht eine lokale Telegram-
    Konfiguration unabhängig von App/Backend — z.B. wenn die normale MQTT/Agent-
    Connect-Verbindung parallel weiterlaufen, Telegram aber lokal fest eingestellt
    sein soll. Die Datei bleibt bestehen und wird bei jedem Start erneut angewendet
    (kein einmaliger Verbrauch); Werte aus der Datei überschreiben, was aktuell in
    config.json steht. Fehlt die Datei, passiert nichts."""
    if not TELEGRAM_CONFIG_FILE.exists():
        return
    try:
        with open(TELEGRAM_CONFIG_FILE) as f:
            data = json.load(f)
    except Exception as e:
        log.error(f'telegram-config.json konnte nicht gelesen werden: {e}')
        return

    applied = [key for key in TELEGRAM_CONFIG_KEYS if key in data]
    for key in applied:
        cfg[key] = data[key]

    if applied:
        save_config(cfg)
        log.info(f'telegram-config.json gefunden, Felder übernommen: {applied}')
    else:
        log.warning(
            'telegram-config.json gefunden, aber keines der erwarteten Felder '
            f'({", ".join(TELEGRAM_CONFIG_KEYS)}) enthalten.'
        )

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

    # Diese Felder werden an jeder Aufrufstelle frisch aus cfg gelesen (kein
    # Caching, kein Prozess-Zustand, der einen Neustart bräuchte — siehe
    # _build_agent_command: cfg.get('hermes_model', '') bzw. call_agent_cli:
    # hermes_reasoning-Parameter aus cfg.get('hermes_reasoning', 'auto')) — ein
    # Modell-/Favoriten-/Reasoning-Wechsel soll sofort mit dem NÄCHSTEN Job
    # wirken, nicht erst nach einem Neustart. Der Neustart verzögert außerdem
    # den nächsten Job um mehrere Sekunden, wodurch Hermes' eigene
    # gateway_state.json währenddessen "stale" werden kann (siehe
    # _hermes_api_server_own_process) — der API-Server-Streaming-Pfad fällt
    # dann unnötig auf CLI zurück.
    RESTART_NOT_NEEDED_KEYS = {'hermes_model', 'hermes_favorite_models', 'hermes_reasoning'}
    restart_relevant_keys = [k for k in updated_keys if k not in RESTART_NOT_NEEDED_KEYS]

    service_name = cfg.get('service_name', '')
    if service_name and restart_relevant_keys:
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
    """Antwortet auf Server-Ping, rotiert Token-B.

    Meldet nur den eigenen lokalen Commit-Hash (reines 'git rev-parse HEAD',
    kein Netzwerk nötig — kann nicht fehlschlagen). Ob das ein Update braucht,
    entscheidet die App selbst durch Vergleich mit dem aktuellen GitHub-Stand,
    statt dass jede Bridge-Installation unabhängig per 'git ls-remote' gegen
    GitHub prüft (fehleranfällig: schlägt der Netzwerk-Check auf einer einzelnen
    Maschine lautlos fehl, wurde bisher "kein Update" gemeldet, obwohl der
    gemeldete Hash längst veraltet war)."""
    try:
        r = requests.post(
            cfg['server_url'] + '/ping.php',
            json={
                'token_b':       cfg['token_b'],
                'bridge_commit': _git_local_commit()[:7],
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


def _nvm_best_node_bin_dir():
    """Höchste per nvm installierte Node-Version unter ~/.nvm/versions/node/
    (bzw. $NVM_DIR). Gibt deren bin/-Verzeichnis zurück, oder None, wenn kein nvm
    bzw. keine Node-Installation darunter gefunden wird."""
    nvm_dir = Path(os.environ.get('NVM_DIR') or (Path.home() / '.nvm')).expanduser()
    versions_dir = nvm_dir / 'versions' / 'node'
    if not versions_dir.is_dir():
        return None

    def _semver_key(d):
        m = re.match(r'v?(\d+)\.(\d+)\.(\d+)', d.name)
        return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)

    candidates = sorted(
        (d for d in versions_dir.iterdir() if d.is_dir() and (d / 'bin' / 'node').exists()),
        key=_semver_key, reverse=True,
    )
    return str(candidates[0] / 'bin') if candidates else None


def apply_nvm_path_fallback():
    """Stellt die höchste per nvm installierte Node.js-Version vorne an PATH, falls
    eine existiert — unabhängig davon, ob cli_command ein bloßer Kommandoname oder ein
    expliziter Pfad ist (z.B. ~/.npm-global/bin/openclaw).

    Grund: Node-CLIs wie openclaw haben ein '#!/usr/bin/env node'-Shebang, das 'node'
    bei JEDEM Aufruf erneut über PATH auflöst — auch wenn cli_command selbst ein
    expliziter Pfad ist. Läuft die Bridge als systemd-User-Service ohne geladenes
    Shell-Profil, fehlt die von nvm.sh normalerweise gesetzte PATH-Erweiterung, und
    'node' fällt auf eine ggf. zu alte global installierte Version zurück — Engine-
    Versionsfehler trotz vorhandener neuerer nvm-Version. Ein bloßer Executable-Name-
    Fallback (nur PATH für 'openclaw' selbst erweitern) reicht dafür NICHT, weil der
    eigentliche Fehler beim Shebang-'node', nicht beim Auffinden von openclaw liegt."""
    bin_dir = _nvm_best_node_bin_dir()
    if not bin_dir:
        return
    os.environ['NVM_BIN'] = bin_dir
    path_parts = os.environ.get('PATH', '').split(os.pathsep)
    if bin_dir not in path_parts:
        os.environ['PATH'] = bin_dir + os.pathsep + os.environ.get('PATH', '')
    log.info(f'nvm: höchste installierte Node-Version vorne an PATH gestellt: {bin_dir}')


def apply_user_local_bin_path():
    """Stellt ~/.local/bin an den PATH, falls vorhanden.

    Als systemd-User-Service startet die Bridge ohne Login-Shell und damit ohne das
    PATH aus dem Shell-Profil — ~/.local/bin (Standardziel von 'pip install --user',
    dort liegt z.B. die hermes-CLI) fehlt dann. Ein bloßer Kommandoname wie 'hermes'
    im cli_command ist sonst nicht auffindbar ("No such file or directory: 'hermes'"),
    obwohl er in der interaktiven Shell einwandfrei funktioniert."""
    bin_dir = str(Path.home() / '.local' / 'bin')
    if not os.path.isdir(bin_dir):
        return
    path_parts = os.environ.get('PATH', '').split(os.pathsep)
    if bin_dir not in path_parts:
        os.environ['PATH'] = bin_dir + os.pathsep + os.environ.get('PATH', '')
        log.info(f'~/.local/bin an PATH gestellt: {bin_dir}')


# ─────────────────────────────────────────────
# Hermes-Backend-Helfer
# ─────────────────────────────────────────────
# Hermes (Nous Research) weicht in zwei Punkten von Claude/Openclaw ab:
#  1. Profil-Auswahl über die Umgebungsvariable HERMES_HOME (kein Per-Call-Flag).
#  2. Kein JSON-Output: `hermes chat -q "…" -Q` liefert den Antworttext plus eine
#     abschließende "session_id: <id>"-Zeile. Wir parsen das zeilenbasiert.
_HERMES_SESSION_LINE_RE = re.compile(r'^session_id:\s*(\S+)\s*$', re.MULTILINE)


def _is_hermes_binary(cmd0):
    return 'hermes' in os.path.basename(cmd0)


def _is_claude_binary(cmd0):
    # Streaming (Phase 1) ist nur für die Claude-CLI implementiert (sauberes
    # NDJSON über --output-format stream-json --verbose). Alle anderen Backends
    # laufen weiter über den nicht-streamenden Pfad, auch wenn die App wants_stream
    # setzt.
    return 'claude' in os.path.basename(cmd0)


def _is_openclaw_binary(cmd0):
    return 'openclaw' in os.path.basename(cmd0)


def _hermes_home_for(profile):
    """Pfad zum Hermes-Home des gewählten Profils. Default-Profil liegt direkt in
    ~/.hermes, benannte Profile unter ~/.hermes/profiles/<name>."""
    base = Path.home() / '.hermes'
    p = (profile or '').strip()
    if not p or p.lower() == 'default':
        return str(base)
    return str(base / 'profiles' / p)


def _hermes_api_server_config(cfg):
    """Liest API_SERVER_*-Variablen aus der .env des aktiven Hermes-Profils
    (Aktivierung laut Hermes-Doku: API_SERVER_ENABLED=true, _HOST, _PORT, _KEY in
    ~/.hermes/.env bzw. ~/.hermes/profiles/<name>/.env, danach `hermes gateway
    restart`). Gibt None zurück, wenn nicht aktiviert oder die Datei fehlt (kein
    Fehler — einfach 'Feature nicht genutzt'), sonst {'host','port','key'}."""
    env_path = Path(_hermes_home_for(cfg.get('hermes_profile', ''))) / '.env'
    if not env_path.is_file():
        return None
    values = {}
    try:
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, val = line.partition('=')
            values[key.strip()] = val.strip().strip('"').strip("'")
    except Exception as e:
        log.warning(f'Could not read Hermes .env at {env_path}: {e}')
        return None

    if values.get('API_SERVER_ENABLED', '').lower() not in ('true', '1', 'yes'):
        return None

    return {
        'host': values.get('API_SERVER_HOST') or '127.0.0.1',
        'port': values.get('API_SERVER_PORT') or '8642',
        'key':  values.get('API_SERVER_KEY') or '',
    }


_HERMES_RUNTIME_STATUS_STALE_TTL_S = 120  # gleicher Wert wie Hermes selbst (gateway/status.py)


def _hermes_runtime_status(profile):
    """Liest {HERMES_HOME}/gateway_state.json des gewählten Profils — von Hermes
    selbst geschrieben (gateway/status.py::write_runtime_status), u.a. mit
    platforms.api_server.state ("connected"/"fatal"/"disconnected"), der PID
    des Gateway-Prozesses und "updated_at". Das ist die einzige zuverlässige,
    LOKALE Quelle dafür, ob DIESES Profil seinen eigenen API-Server wirklich
    gestartet hat. Gibt None zurück, wenn die Datei fehlt/kaputt ist (kein
    Fehler — z.B. Gateway nie gestartet)."""
    state_path = Path(_hermes_home_for(profile)) / 'gateway_state.json'
    if not state_path.is_file():
        return None
    try:
        return json.loads(state_path.read_text())
    except Exception as e:
        log.warning(f'Could not read Hermes runtime status at {state_path}: {e}')
        return None


def _hermes_api_server_own_process(cfg):
    """True, wenn gateway_state.json bestätigt, dass DIESES Profil (nicht ein
    anderer Hermes-Account auf demselben Server) seinen eigenen api_server-
    Adapter erfolgreich gebunden hat und der Gateway-Prozess noch lebt und
    kürzlich seinen Status geschrieben hat.

    Hintergrund (Recherche gegen den offiziellen Quellcode, github.com/
    NousResearch/hermes-agent): Bindet der API-Server beim Start einen bereits
    belegten Port (EADDRINUSE), weicht er NICHT automatisch auf einen anderen
    Port aus — gateway/platforms/api_server.py::connect() fängt den OSError,
    setzt einen dauerhaften, nicht erneut versuchten Fehler
    (_set_fatal_error("api_server_port_in_use", ..., retryable=False)) und
    gibt False zurück; der api_server-Adapter dieses Profils bleibt bis zu
    einem manuellen `/platform resume api_server` (nach Port-Änderung) down.
    Laufen mehrere Hermes-Accounts mit identischer (Default-)API_SERVER_PORT
    auf demselben Server, gewinnt nur EINER den Bind — die anderen bekommen
    diesen Fehler.

    Ein reiner /health-Request auf den in der .env konfigurierten Host:Port
    (wie zuvor) kann das NICHT unterscheiden: er liefert für die verlierenden
    Profile trotzdem 200 OK, weil er in Wirklichkeit den GEWINNER-Account
    beantwortet (/health enthält keinerlei Profil-/Account-Kennung). Ohne
    diesen Check würde die Bridge also für 2 von 3 Accounts unbemerkt mit dem
    FALSCHEN Hermes-Agenten sprechen. gateway_state.json wird lokal vom
    jeweils eigenen Gateway-Prozess über sich selbst geschrieben und ist damit
    unabhängig vom belegten Port eindeutig diesem Profil zuordenbar."""
    status = _hermes_runtime_status(cfg.get('hermes_profile', ''))
    if status is None:
        return False
    platform_state = ((status.get('platforms') or {}).get('api_server') or {}).get('state')
    if platform_state != 'connected':
        return False

    updated_at = status.get('updated_at')
    if isinstance(updated_at, str):
        try:
            ts = datetime.fromisoformat(updated_at.replace('Z', '+00:00'))
            age_s = (datetime.now(timezone.utc) - ts).total_seconds()
            if age_s > _HERMES_RUNTIME_STATUS_STALE_TTL_S:
                log.info(f'Hermes gateway_state.json is stale ({age_s:.0f}s) — treating api_server as not owned.')
                return False
        except Exception:
            pass  # kaputtes/unbekanntes Format ignorieren, nicht deswegen scheitern

    pid = status.get('pid')
    if not isinstance(pid, int):
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False  # Prozess tot — Status-Datei überlebt den Absturz
    return True


def _hermes_api_server_ready(cfg):
    """Kombiniert Config-Check + Eigentums-Check (gateway_state.json) + Live-
    Probe (GET /health, kurzer Timeout). Der Eigentums-Check MUSS vor dem
    /health-Request laufen, sonst könnte dieser bei einem Port-Konflikt
    zwischen mehreren Hermes-Accounts den API-Server eines ANDEREN Accounts
    treffen (siehe _hermes_api_server_own_process). Eine .env, die "enabled"
    sagt, garantiert außerdem nicht, dass der Gateway seit der Änderung auch
    neu gestartet wurde oder gerade läuft. Gibt die Server-Config zurück, wenn
    WIRKLICH nutzbar, sonst None (Aufrufer fällt dann auf den CLI-Pfad zurück)."""
    server_cfg = _hermes_api_server_config(cfg)
    if server_cfg is None:
        return None
    if not _hermes_api_server_own_process(cfg):
        log.info('Hermes API server not owned by this profile (gateway_state.json) — falling back to CLI.')
        return None
    try:
        url = f"http://{server_cfg['host']}:{server_cfg['port']}/health"
        r = requests.get(url, timeout=2)
        if r.status_code != 200:
            log.info(f'Hermes API server /health returned {r.status_code} — falling back to CLI.')
            return None
    except Exception as e:
        log.info(f'Hermes API server not reachable ({e}) — falling back to CLI.')
        return None
    return server_cfg


def _openclaw_config():
    """Liest die Gateway-Einstellungen aus OpenClaws eigener Config-Datei
    (~/.openclaw/openclaw.json) — analog zu _hermes_api_server_config. Aktivierung
    laut offizieller Doku (docs.openclaw.ai/gateway/openai-http-api, 2026-07):
    `openclaw config set gateway.http.endpoints.chatCompletions.enabled true` +
    `gateway.auth.mode`/`gateway.auth.token`. Gibt None zurück, wenn die Datei fehlt,
    der Endpunkt nicht aktiviert ist, oder kein Token gesetzt ist (kein Fehler —
    einfach 'Feature nicht genutzt')."""
    config_path = Path.home() / '.openclaw' / 'openclaw.json'
    if not config_path.is_file():
        return None
    try:
        data = json.loads(config_path.read_text())
    except Exception as e:
        log.warning(f'Could not read/parse OpenClaw config at {config_path}: {e}')
        return None

    gateway    = data.get('gateway') or {}
    http_cfg   = gateway.get('http') or {}
    endpoints  = http_cfg.get('endpoints') or {}
    chat_compl = endpoints.get('chatCompletions') or {}
    if not chat_compl.get('enabled'):
        return None

    auth  = gateway.get('auth') or {}
    token = auth.get('token') or auth.get('password') or ''
    if not token:
        return None

    return {
        'host':  '127.0.0.1',
        'port':  gateway.get('port') or 18789,
        'token': token,
    }


def _openclaw_streaming_ready(cfg):
    """Kombiniert Config-Check + Live-Probe. GET /v1/models ist der einzige
    dokumentiert bestätigte einfache GET-Endpunkt (kein /health für OpenClaw
    verifiziert, anders als bei Hermes) — dient hier als Liveness+Auth-Check. Gibt
    die Server-Config zurück, wenn WIRKLICH nutzbar, sonst None (Aufrufer fällt dann
    auf den CLI-Pfad zurück)."""
    server_cfg = _openclaw_config()
    if server_cfg is None:
        return None
    try:
        url = f"http://{server_cfg['host']}:{server_cfg['port']}/v1/models"
        r = requests.get(url, headers={'Authorization': f"Bearer {server_cfg['token']}"}, timeout=2)
        if r.status_code != 200:
            log.info(f'OpenClaw gateway /v1/models returned {r.status_code} — falling back to CLI.')
            return None
    except Exception as e:
        log.info(f'OpenClaw gateway not reachable ({e}) — falling back to CLI.')
        return None
    return server_cfg


_HERMES_REASONING_HEADER_RE = re.compile(r'^\s*┌─\s*Reasoning\b[^\n]*┐\s*\n+')


def _format_hermes_reasoning_block(answer):
    """Formt Hermes' 'Reasoning'-Panel-Ausgabe (Box-Kopfzeile ┌─ Reasoning ─...─┐,
    OHNE schließende Boxkante beim Pipe-Capture — kein echtes TTY, siehe
    call_agent_cli) in einen fett beschrifteten, normal fließenden Absatz vor der
    eigentlichen Antwort um ("**🧠 Reasoning:** ...") — Nutzerwunsch nach einer
    ERSTEN Version mit einzelliger Markdown-Tabelle: die zwang den ganzen
    Reasoning-Text in eine Tabellenzelle (Scrollen statt normalem Zeilenumbruch),
    das hier vermeidet das komplett. Bold wird auf beiden Plattformen bereits
    ohne jede Client-Änderung korrekt gerendert.

    Kalibriert an zwei echten Beispielen (2026-08-22): in BEIDEN erschien der
    Reasoning-Text durch ein Live-Redraw-Artefakt zweimal direkt hintereinander
    (einmal unregelmäßig umgebrochen, einmal als saubere Kopie) — aber OHNE
    verlässlichen Absatztrenner (mal Leerzeile, mal einzelner Zeilenumbruch, mal
    GAR KEIN Trenner zwischen den beiden Kopien). Ein absatzbasierter Split
    (frühere Version dieser Funktion) versagte deshalb beim zweiten Beispiel.
    Robuster Ersatz: sucht direkt nach der kürzesten selbstverkettenden
    Textwiederholung (whitespace-normalisiert) am Anfang von `rest`, unabhängig
    von Zeilenumbrüchen — die Grenze wird über eine Normalisierungs-Index-
    Abbildung zurück auf die Roh-Position gemappt, ab der der Rest (die
    eigentliche Antwort) beginnt. Liefert bei jeder Unsicherheit (keine
    Wiederholung gefunden) den Text UNVERÄNDERT zurück — lieber unformatiert als
    falsch zerschnitten. MUSS an weiteren echten Antworten nachgeprüft werden."""
    m = _HERMES_REASONING_HEADER_RE.match(answer)
    if not m:
        if 'Reasoning' in answer[:200]:
            # Nur loggen, wenn überhaupt ein Hinweis auf eine Reasoning-Box im
            # Text steckt — sonst würde JEDE ganz normale Antwort (kein Override
            # aktiv) diese Zeile erzeugen. Repr(), damit Steuerzeichen/exotische
            # Unicode-Box-Zeichen im Log sichtbar werden statt unsichtbar zu
            # verschwinden — genau dafür ist diese Diagnose gedacht.
            log.info(f'_format_hermes_reasoning_block: "Reasoning" im Text, aber Kopfzeile passt nicht. '
                     f'Erste 200 Zeichen: {answer[:200]!r}')
        return answer
    rest = answer[m.end():]

    # Whitespace-normalisierte Kopie MIT Index-Abbildung zurück auf `rest` —
    # normalized[i] entspricht rest[:index_map[i]] (index_map[i] = Roh-Position
    # direkt NACH dem Zeichen/der Whitespace-Lücke, die normalized[i] erzeugt hat).
    norm_chars = []
    index_map = []
    i = 0
    n_rest = len(rest)
    while i < n_rest:
        c = rest[i]
        if c.isspace():
            j = i
            while j < n_rest and rest[j].isspace():
                j += 1
            if norm_chars and norm_chars[-1] != ' ':
                norm_chars.append(' ')
                index_map.append(j)
            i = j
        else:
            norm_chars.append(c)
            i += 1
            index_map.append(i)
    normalized = ''.join(norm_chars).strip()
    n = len(normalized)

    reasoning_text = None
    final_answer = None
    for k in range(20, n // 2 + 1):
        if normalized[k - 1] == ' ':
            continue  # Wortgrenze bevorzugen, nicht mitten im Wort splitten
        if normalized[:k] == normalized[k:2 * k]:
            reasoning_text = normalized[:k]
            end_idx = min(2 * k, len(index_map)) - 1
            original_end = index_map[end_idx] if end_idx >= 0 else 0
            final_answer = rest[original_end:].strip()
            break

    if reasoning_text is None or not final_answer:
        log.info(f'_format_hermes_reasoning_block: Kopfzeile erkannt, aber keine Selbstwiederholung gefunden. '
                 f'Normalisierter Rest (erste 300 Zeichen): {normalized[:300]!r}')
        return answer

    log.info(f'_format_hermes_reasoning_block: umformatiert ({len(reasoning_text)} Zeichen Reasoning, '
              f'{len(final_answer)} Zeichen Antwort).')
    return f'**🧠 Reasoning:** {reasoning_text}\n\n{final_answer}'


def _parse_hermes_output(raw, stderr=''):
    """Trennt Antworttext und Session-ID aus der Hermes-Ausgabe.

    WICHTIG: Hermes schreibt die "session_id:"-Zeile auf STDERR, nicht auf stdout
    (empirisch verifiziert 2026-07-22: `hermes chat -q … -Q 2>/dev/null` liefert nur
    den Antworttext, die session_id-Zeile erscheint ausschließlich auf stderr).
    stdout enthält also bereits die reine Antwort. Der stdout-Fallback bleibt
    erhalten, falls Hermes das Format später ändert.

    Rückgabe (answer, session_id); answer=None bei leerer Antwort."""
    sid = None
    # 1) Primärquelle: stderr
    last = None
    for last in _HERMES_SESSION_LINE_RE.finditer(stderr or ''):
        pass  # letzte Übereinstimmung gewinnt
    if last:
        sid = last.group(1)

    # 2) Fallback: stdout — dort muss die Zeile zusätzlich abgeschnitten werden.
    answer = raw or ''
    if sid is None:
        last = None
        for last in _HERMES_SESSION_LINE_RE.finditer(answer):
            pass
        if last:
            sid = last.group(1)
            answer = answer[:last.start()]

    answer = answer.strip()
    if not answer:
        log.error(f'Hermes output has no answer text: {(raw or "")[:200]}')
        return None, None
    answer = _format_hermes_reasoning_block(answer)
    return answer, sid


def _extract_hermes_session_id_only(stdout, stderr):
    """Wie _parse_hermes_output, aber gibt NUR die Session-ID zurück und verlangt
    KEINEN nicht-leeren Antworttext. Wird für den /reasoning-Vorab-Aufruf gebraucht
    (siehe _apply_hermes_reasoning) — dessen Bestätigungstext im Quiet-Modus (-Q)
    leer sein kann; _parse_hermes_output würde dann fälschlich (None, None)
    liefern und die gerade erst erzeugte Session-ID verwerfen."""
    last = None
    for last in _HERMES_SESSION_LINE_RE.finditer(stderr or ''):
        pass
    if last:
        return last.group(1)
    last = None
    for last in _HERMES_SESSION_LINE_RE.finditer(stdout or ''):
        pass
    if last:
        return last.group(1)
    return None


# Merkt sich pro Hermes-Session, welches Reasoning-Level dort zuletzt per
# /reasoning gesetzt wurde — Prozessspeicher, leert sich bei jedem Bridge-
# Neustart (unproblematisch: eine neue Session hat ohnehin noch keinen
# gemerkten Wert, siehe _apply_hermes_reasoning). Key ist die Hermes-Session-ID,
# oder '__new__' für eine noch nicht existierende Session (erster Aufruf einer
# neuen Unterhaltung mit sofort gesetztem Override).
_hermes_reasoning_by_session = {}


def _apply_hermes_reasoning(cfg, level, resume_id):
    """Setzt das Reasoning-Level für eine Hermes-Session per In-Chat-Slash-Befehl
    `/reasoning <level>` (none|minimal|low|medium|high|xhigh|max|ultra) — Hermes
    hat dafür KEINEN Chat-Zeitpunkt-Flag (siehe `hermes chat --help`), nur diesen
    interaktiven Befehl, daher ein eigener, vorgeschalteter CLI-Aufruf statt
    einem zusätzlichen Argument am eigentlichen Nachrichten-Aufruf. Wird nur
    ausgeführt, wenn der gewünschte Wert vom zuletzt für diese Session gesetzten
    abweicht (_hermes_reasoning_by_session) — erspart einen weiteren CLI-Aufruf
    bei jeder Folgenachricht mit unverändertem Level.

    resume_id kann leer sein (noch keine Session) — in dem Fall ERZEUGT dieser
    Aufruf eine neue Hermes-Session; die daraus extrahierte Session-ID wird
    zurückgegeben und MUSS vom Aufrufer als session_id_override für den
    nachfolgenden eigentlichen Nachrichten-Aufruf verwendet werden, sonst würde
    dieser eine GANZ NEUE (zweite) Session starten statt die gerade erzeugte
    fortzusetzen.

    Rückgabe: die zu verwendende Session-ID (neu erzeugt oder unverändert
    resume_id) — auch bei Fehlern/Nichtstun, nie None, damit der Aufrufer immer
    einfach damit weiterarbeiten kann."""
    session_key = resume_id or '__new__'
    if _hermes_reasoning_by_session.get(session_key) == level:
        return resume_id

    binary = _cli_binary(cfg)
    if not binary:
        return resume_id
    env = os.environ.copy()
    env.update(cfg.get('cli_env', {}))
    env['HERMES_HOME'] = _hermes_home_for(cfg.get('hermes_profile', ''))
    cwd = os.path.expanduser(cfg['cli_working_dir']) if cfg.get('cli_working_dir') else None
    cmd = [binary, 'chat', '-Q', '--yolo', '--accept-hooks', '-q', f'/reasoning {level}']
    session_param = cfg.get('cli_session_id_param', '')
    if resume_id and session_param:
        cmd += [session_param, resume_id]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=cwd, timeout=60)
    except Exception as e:
        log.error(f'_apply_hermes_reasoning: {e}')
        return resume_id
    if result.returncode != 0:
        log.error(f'_apply_hermes_reasoning: hermes exited {result.returncode}: {result.stderr[:300]}')
        return resume_id

    extracted_sid = _extract_hermes_session_id_only(result.stdout.strip(), result.stderr)
    new_resume_id = extracted_sid or resume_id
    _hermes_reasoning_by_session[new_resume_id or '__new__'] = level
    if extracted_sid:
        store_session_id(extracted_sid)
    log.info(f'Hermes reasoning set to "{level}" for session {new_resume_id or "(new)"}')
    return new_resume_id


# ─────────────────────────────────────────────
# System-Prompt-Injection (Backends ohne eigenes System-Prompt-Flag)
# ─────────────────────────────────────────────
# Openclaw und Hermes haben kein Per-Call-System-Prompt-Flag (bei Claude: --system-prompt).
# Der System-Prompt wird dort dem eigentlichen Prompt vorangestellt und landet damit im
# Gesprächsverlauf — bei fortsetzbaren Sessions (Hermes --resume) bliebe er sonst
# scheinbar dauerhaft gültig. Die Einrahmung macht den Einmal-Charakter explizit.
#
# Die Marker sind zugleich eindeutige Trennzeichen fürs Wieder-Entfernen im Verlauf-Tab
# (_strip_injected_system_prompt) — dadurch werden auch INDIVIDUELL angepasste
# System-Prompts sauber erkannt, was mit dem alten Exact-Match-Verfahren nicht ging.
# Frame und Strip werden bewusst aus denselben Konstanten gebaut, damit sie nicht
# auseinanderlaufen können.
_ONE_TIME_FRAME_MARKERS = {
    'DE': ('[Hinweis: Die folgende Anweisung gilt NUR für diese eine Antwort, '
           'nicht für künftige Nachrichten in diesem Gespräch]',
           '[Ende der einmaligen Anweisung]'),
    'EN': ('[Note: The following instruction applies ONLY to this one response, '
           'not to future messages in this conversation]',
           '[End of one-time instruction]'),
}

_ONE_TIME_FRAME_STRIP_RE = re.compile(
    '|'.join(
        re.escape(start) + r'\n.*?\n' + re.escape(end) + r'\n*'
        for start, end in _ONE_TIME_FRAME_MARKERS.values()
    ),
    re.DOTALL,
)


def _frame_one_time_system_prompt(system_prompt, lang):
    start, end = _ONE_TIME_FRAME_MARKERS.get(lang, _ONE_TIME_FRAME_MARKERS['EN'])
    return f'{start}\n{system_prompt}\n{end}'


def _strip_one_time_frame(text):
    """Entfernt einen eingerahmten System-Prompt (beide Sprachen, auch angepasste)."""
    return _ONE_TIME_FRAME_STRIP_RE.sub('', text, count=1).lstrip()


# ─────────────────────────────────────────────
# Agent call (CLI)
# ─────────────────────────────────────────────
# Flags, die die Bridge für Claude jetzt SELBST verwaltet (siehe _build_agent_command):
# Output-Format UND --dangerously-skip-permissions (zwingend für headless Betrieb ohne
# TTY — sonst hängt die CLI bei jedem Permission-Prompt, z.B. bei Tool-/MCP-Nutzung).
# Ein Wert aus einer älteren, noch gespeicherten cli_extra_params-Konfiguration (z.B.
# "--output-format json --dangerously-skip-permissions" aus dem alten Preset-Default)
# wird beim Zusammenbauen herausgefiltert, statt doppelt oder widersprüchlich neben dem
# bridge-eigenen Wert zu landen. Alles andere in cli_extra_params (z.B. --mcp-config,
# --allowedTools) bleibt unverändert erhalten — nur diese Namen werden erkannt.
_CLAUDE_MANAGED_FLAGS_WITH_VALUE = {'--output-format'}
_CLAUDE_MANAGED_FLAGS_BARE       = {'--verbose', '--include-partial-messages', '--dangerously-skip-permissions'}

# Gleiches Prinzip (2026-07-24) jetzt auch für Hermes und OpenClaw: beide brauchen
# ihre "Standard"-Flags zwingend für headless Betrieb (kein optionales Extra), die
# Bridge hängt sie deshalb selbst an. cli_extra_params ist bei beiden Backends jetzt
# ebenfalls standardmäßig leer, nur für echte Sonderfälle gedacht.
# Hermes: -Q (ruhige/nicht-TUI-Ausgabe, nötig für sauberes stdout-Parsing),
#         --yolo (Tool-Permissions ohne Rückfrage), --accept-hooks (Hooks ohne Rückfrage).
_HERMES_MANAGED_FLAGS_BARE = {'-Q', '--yolo', '--accept-hooks'}
# OpenClaw: --json ist zwingend für die JSON-basierte Antwort-/Session-ID-Auswertung
# in call_agent_cli (cli_answer_output_field nutzt Dot-Notation auf JSON).
_OPENCLAW_MANAGED_FLAGS_BARE = {'--json'}


def _file_note_prefix(files):
    """Text-Hinweis auf mitgeschickte Dateien, wie im CLI-Fallback unten (kein
    cli_file_param konfiguriert) — der Agent liest die Datei selbst über sein
    eigenes Tool (z.B. Read/vision_analyze). Auch von den Hermes-/OpenClaw-
    Streaming-Funktionen genutzt: deren APIs kennen kein natives Datei-
    Attachment (nur Text-Messages), der Prompt-Hinweis ist dort die EINZIGE
    Möglichkeit, live verifiziert 2026-07 (siehe Bridge-Log 'attachment
    present' + AC-Chat-Verlauf mit "The user sent the following file...")."""
    if not files:
        return ''
    return '\n'.join(
        f'The user sent the following file, please read and process it: {Path(fp).resolve()}'
        for fp in files
    )


def _build_agent_command(cfg, prompt, system_prompt='', files=None, session_id_override=None,
                          streaming=False, app_continuation=None):
    """Baut die vollständige CLI-Kommandozeile (Session-, System-Prompt-, Datei- und
    Extra-Parameter) für den konfigurierten Agenten. Gemeinsam genutzt vom
    nicht-streamenden Pfad (call_agent_cli) und vom Streaming-Pfad
    (call_agent_cli_streaming), damit die Kommandobildung nur an EINER Stelle lebt.

    Für die Claude-CLI verwaltet die Bridge das Output-Format jetzt SELBST (json bzw.
    stream-json+verbose+include-partial-messages je nach `streaming`) — das Feld
    "Extra-Parameter" ist dafür nicht mehr zuständig (neuer Default: leer). Alte,
    bereits gespeicherte cli_extra_params mit "--output-format ..."/"--verbose"/
    "--include-partial-messages" werden beim Bauen herausgefiltert (Rückwärtskompatibilität —
    kein Doppel-Flag, kein Widerspruch), alles andere (z.B. MCP-relevante Flags) bleibt
    unverändert. Andere Backends (openclaw/hermes) sind von dieser Verwaltung NICHT
    betroffen — ihre cli_extra_params laufen unangetastet durch wie bisher.

    Rückgabe: (cmd, env, cwd, timeout, is_hermes) oder None bei fehlendem cli_command.
    Nebenwirkung: der openclaw-„neue Session"-Zweig ruft store_session_id() (wie zuvor
    innerhalb von call_agent_cli) — unverändertes Verhalten.

    app_continuation: steuert NUR die Log-Beschriftung ("(App-Fortsetzen)"), nicht
    das Verhalten — None (Default) fällt auf die alte Heuristik zurück (truthy
    session_id_override = App-Fortsetzen). Explizit False nötig, wenn
    session_id_override zwar gesetzt ist, aber NICHT vom App-Request stammt,
    sondern z.B. von _apply_hermes_reasoning innerhalb desselben Turns frisch
    erzeugt wurde (call_agent_cli überschreibt session_id_override in dem Fall) —
    sonst würde eine frisch per "Neues Gespräch" gestartete Session fälschlich als
    "App-Fortsetzen" geloggt (siehe call_agent_cli)."""
    if not cfg.get('cli_command'):
        log.error('No cli_command configured. Set it via the web backend (Bridge konfigurieren).')
        return None

    cmd = shlex.split(os.path.expanduser(cfg['cli_command']))
    is_hermes = _is_hermes_binary(cmd[0])
    is_claude = _is_claude_binary(cmd[0])
    is_openclaw = _is_openclaw_binary(cmd[0])

    # Session-Handling
    session_param    = cfg.get('cli_session_id_param', '')
    session_id_field = cfg.get('cli_session_id_output_field', '')
    resume_id        = session_id_override or _current_session_id

    if session_param:
        if resume_id:
            is_app_continuation = bool(session_id_override) if app_continuation is None else app_continuation
            origin = ' (App-Fortsetzen)' if is_app_continuation else ''
            cmd += [session_param, resume_id]
            log.info(f'Continuing session: {resume_id}{origin}')
        elif is_hermes:
            # Hermes: neue Session ohne Flag; die ID wird von Hermes selbst vergeben
            # und aus der Ausgabe (session_id:-Zeile) gelesen. NICHT wie openclaw eine
            # UUID vorgeben — --resume erwartet eine EXISTIERENDE Session.
            log.info('Hermes: starting new session (no resume)')
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
            # Kein System-Prompt-Flag (Openclaw/Hermes) → voranstellen, aber als
            # einmalig gültig eingerahmt (siehe _frame_one_time_system_prompt).
            framed = _frame_one_time_system_prompt(system_prompt, cfg.get('lang', 'DE'))
            log.info(f'No sp_param configured — prepending framed (one-time) system prompt ({len(system_prompt)} chars): "{system_prompt[:120]}"')
            prompt = f'{framed}\n\n{prompt}' if prompt else framed
    else:
        log.info('No system prompt passed to agent CLI.')

    # Von der Bridge verwaltete Flags des jeweiligen Backends bestimmen (leere Sets
    # für alle anderen Backends — deren cli_extra_params bleibt komplett unangetastet).
    if is_claude:
        managed_with_value, managed_bare = _CLAUDE_MANAGED_FLAGS_WITH_VALUE, _CLAUDE_MANAGED_FLAGS_BARE
    elif is_hermes:
        managed_with_value, managed_bare = set(), _HERMES_MANAGED_FLAGS_BARE
    elif is_openclaw:
        managed_with_value, managed_bare = set(), _OPENCLAW_MANAGED_FLAGS_BARE
    else:
        managed_with_value, managed_bare = set(), set()

    extra_args = shlex.split(cfg.get('cli_extra_params', ''))
    if managed_with_value or managed_bare:
        # Rückwärtskompatibilität: von der Bridge verwaltete Flags aus einer alten
        # Konfiguration herausfiltern (siehe Modul-Kommentar oben). Alles andere
        # (MCP-relevante Flags etc.) bleibt erhalten.
        filtered = []
        i = 0
        while i < len(extra_args):
            arg = extra_args[i]
            if arg in managed_with_value:
                i += 2   # Flag + zugehöriger Wert überspringen
                continue
            if arg in managed_bare:
                i += 1
                continue
            filtered.append(arg)
            i += 1
        extra_args = filtered

    for arg in extra_args:
        cmd.append(os.path.expanduser(arg))

    if is_claude:
        # Bridge-verwaltetes Output-Format + Permission-Skip — siehe Modul-Kommentar
        # oben. Nur Claude kennt diese Flags; openclaw/hermes bleiben unangetastet
        # (kein is_claude-Zweig). --dangerously-skip-permissions unabhängig vom
        # Streaming-Modus, da headless immer nötig.
        if streaming:
            cmd += ['--output-format', 'stream-json', '--verbose', '--include-partial-messages']
        else:
            cmd += ['--output-format', 'json']
        cmd.append('--dangerously-skip-permissions')
        # Modell-Alias optional fest vorgeben (ConfigView.swift, "" = altes
        # Verhalten, kein Flag). Bewusst dieselbe Whitelist wie serverseitig in
        # ios_app_endpoint.php save_bridge_config — auch bei einem alten/
        # manipulierten config.json kein beliebiger Wert an die CLI.
        claude_model = cfg.get('claude_model', '')
        if claude_model in ('sonnet', 'opus', 'haiku', 'fable'):
            cmd += ['--model', claude_model]
        # Reasoning-Effort (6 Stufen wie hermes_reasoning): "auto" (Standard)
        # hängt nichts an — CLI/Modell entscheidet selbst. Sonst direktes
        # --effort-Flag auf dem eigentlichen Aufruf (siehe Claude Code CLI docs,
        # code.claude.com/docs/en/cli-reference: "Options: low, medium, high,
        # xhigh, max, or ultracode. ... Overrides the effortLevel setting for
        # this session and does not persist") — anders als Hermes' /reasoning
        # KEIN separater Voraufruf nötig. Defensive Whitelist wie serverseitig
        # in ios_app_endpoint.php save_bridge_config, falls ein altes/
        # manipuliertes config.json noch den alten Tri-State-Wert enthält.
        claude_reasoning = cfg.get('claude_reasoning') or 'auto'
        if claude_reasoning in ('low', 'medium', 'high', 'xhigh', 'max'):
            cmd += ['--effort', claude_reasoning]
    elif is_hermes:
        # Bridge-verwaltete Standard-Flags — siehe Modul-Kommentar oben. `streaming`
        # ändert hier (noch) nichts; Hermes-Streaming ist ein separates, späteres
        # Vorhaben über den Hermes-API-Server, nicht über diesen CLI-Aufruf.
        cmd += ['-Q', '--yolo', '--accept-hooks']
        # Modellwechsel (ConfigView.swift/index.php, Favoriten-UI): -m/--model ist
        # ein reiner Chat-Zeitpunkt-Flag (siehe `hermes chat --help`) — KEINE
        # Mutation der Hermes-eigenen config.yaml nötig (kein `config set
        # model.default ...`). Leer/ungesetzt → Hermes bleibt beim eigenen, im
        # Profil hinterlegten Default.
        hermes_model = cfg.get('hermes_model', '')
        if hermes_model:
            cmd += ['-m', hermes_model]
    elif is_openclaw:
        cmd.append('--json')

    # Dateianhänge einbauen
    file_param = cfg.get('cli_file_param', '')
    if files:
        if file_param:
            for fp in files:
                cmd += [file_param, fp]
        else:
            # Kein CLI-Flag konfiguriert: Dateipfad im Prompt nennen.
            # Claude Code CLI hat den Read-Tool und liest die Datei selbst.
            file_note = _file_note_prefix(files)
            prompt = f'{file_note}\n\n{prompt}' if prompt else file_note

    prompt_param = cfg.get('cli_prompt_param', '')
    if prompt_param:
        cmd += [prompt_param, prompt]
    else:
        cmd.append(prompt)

    env = os.environ.copy()
    env.update(cfg.get('cli_env', {}))
    if is_hermes:
        # Profil-Auswahl: HERMES_HOME zeigt auf das Home des gewählten Hermes-Profils.
        env['HERMES_HOME'] = _hermes_home_for(cfg.get('hermes_profile', ''))
        log.info(f'Hermes profile home: {env["HERMES_HOME"]}')

    cwd     = os.path.expanduser(cfg['cli_working_dir']) if cfg.get('cli_working_dir') else None
    timeout = cfg.get('cli_timeout', 600)

    return cmd, env, cwd, timeout, is_hermes


def call_agent_cli(cfg, prompt, system_prompt='', files=None, session_id_override=None,
                    hermes_reasoning=None):
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

    hermes_reasoning: nur bei Hermes relevant, persistent am Profil gesetzter
    Override (none|minimal|low|medium|high|xhigh|max|ultra, ac_profiles.
    hermes_reasoning) — bleibt bis der User ihn wieder auf 'auto' stellt.
    'auto'/leer = kein Override,
    Hermes entscheidet selbst. Wird per _apply_hermes_reasoning VOR dem
    eigentlichen Aufruf gesetzt (eigener CLI-Voraufruf, da Hermes dafür keinen
    Chat-Zeitpunkt-Flag hat) — erzeugt diese Session dabei gerade erst neu
    (noch kein session_id_override/_current_session_id vorhanden), wird die neu
    erzeugte Session-ID hier als session_id_override übernommen, damit der
    eigentliche Aufruf unten dieselbe Session fortsetzt statt eine zweite,
    unabhängige zu erzeugen.

    Rückgabe: (answer, session_id) — answer ist None bei Fehler; session_id ist die aus
    dem Output extrahierte ID oder None (kein Feld konfiguriert / nicht vorhanden).
    """
    # Vor einem möglichen Overwrite unten festhalten, ob DIESER Aufruf wirklich vom
    # App-Request als Fortsetzung gedacht war — sonst würde eine frisch von
    # _apply_hermes_reasoning im selben Turn erzeugte Session fälschlich als
    # "App-Fortsetzen" geloggt (siehe _build_agent_command).
    app_continuation = bool(session_id_override)
    if _is_hermes_binary(_cli_binary(cfg)) and hermes_reasoning and hermes_reasoning != 'auto':
        resume_id = session_id_override or _current_session_id
        session_id_override = _apply_hermes_reasoning(cfg, hermes_reasoning, resume_id)

    built = _build_agent_command(cfg, prompt, system_prompt, files, session_id_override,
                                  app_continuation=app_continuation)
    if built is None:
        return None, None
    cmd, env, cwd, timeout, is_hermes = built
    # _build_agent_command berechnet session_id_field nur für seinen eigenen internen
    # Gebrauch (Resume-Entscheidung) und gibt es nicht zurück — hier unten aber für die
    # JSON-Antwort-Auswertung erneut benötigt. Reiner cfg-Read, keine Nebenwirkung.
    session_id_field = cfg.get('cli_session_id_output_field', '')

    log.info(f'Calling CLI agent: {cmd[0]} (timeout={timeout}s, cwd={cwd})')
    log.info(f'CLI args: {cmd[1:]}')
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

        extracted_sid = None
        if is_hermes:
            # Diagnose für _format_hermes_reasoning_block (Trennmuster Reasoning/
            # Antwort variiert von Fall zu Fall, siehe dortiger Kommentar) — repr(),
            # damit Zeilenumbrüche/Whitespace sichtbar bleiben statt im Log optisch
            # zu verschwinden. Bewusst großzügig (1500 Zeichen), nicht nur 200-300.
            log.info(f'Hermes raw stdout (repr, erste 1500 Zeichen): {raw[:1500]!r}')
            # Hermes: zeilenbasiert (kein JSON). Antwort steht auf stdout, die
            # session_id-Zeile auf stderr — beides übergeben (siehe Funktions-Doku).
            answer, extracted_sid = _parse_hermes_output(raw, result.stderr)
            if answer is None:
                return None, None
            if extracted_sid:
                store_session_id(extracted_sid)
        else:
            # JSON-Output parsen wenn session_id_field gesetzt ODER answer_field Dot-Notation enthält
            answer_field = cfg.get('cli_answer_output_field', 'result')
            parse_json   = bool(session_id_field) or '.' in answer_field
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


def put_partial(cfg, job_id, partial):
    """Schreibt eine Zwischenausgabe (wachsender Text) eines noch laufenden Jobs an
    put_partial.php. Token-B wird bewusst NICHT rotiert (siehe put_partial.php) — die
    Antwort enthält kein token_b_new, wir speichern nichts. Fire-and-forget: Fehler
    werden nur geloggt, der Haupt-Job-Flow läuft davon unberührt weiter."""
    try:
        requests.post(
            cfg['server_url'] + '/put_partial.php',
            json={'token_b': cfg['token_b'], 'job_id': job_id, 'partial': partial},
            timeout=5,
        )
    except Exception as e:
        log.warning(f'put_partial failed for job #{job_id} (ignored): {e}')


def call_agent_cli_streaming(cfg, prompt, system_prompt='', files=None,
                             session_id_override=None, on_partial=None):
    """Streaming-Variante von call_agent_cli — NUR für die Claude-CLI (Phase 1).

    Baut dasselbe Kommando wie der nicht-streamende Pfad (_build_agent_command),
    stellt aber das Ausgabeformat auf '--output-format stream-json --verbose
    --include-partial-messages' um und liest die NDJSON-Events zeilenweise per Popen.

    WICHTIG (2026-07-23, nach erstem Live-Test korrigiert): OHNE
    --include-partial-messages liefert die Claude-CLI pro Gesprächsrunde nur EINE
    komplette Assistant-Message (kein Zeichen-für-Zeichen-Wachstum) — bei
    MCP-Tool-Nutzung sind die Zwischen-Nachrichten zudem meist reine tool_use-Blöcke
    ohne Text. Mit dem Flag kommen zusätzlich 'stream_event'-Zeilen mit
    content_block_delta/text_delta — das sind die tatsächlich wachsenden
    Textstücke, die hier akkumuliert werden. on_partial(text) wird gedrosselt (~1,5s)
    aufgerufen.

    Die finale Antwort + session_id kommen weiterhin aus der letzten
    '{"type":"result",...}'-Zeile, exakt wie im nicht-streamenden Pfad ausgewertet
    (cli_answer_output_field / cli_session_id_output_field) — unverändert von diesem
    Fix. Rückgabe: (answer, session_id) — answer=None bei Fehler, sodass der Aufrufer
    auf den nicht-streamenden Pfad zurückfallen kann.

    Tool-Nutzung (2026-07-26, analog zur Hermes-Tool-Progress-Anzeige): bei
    content_block_start mit content_block.type=="tool_use" wird eine kosmetische
    "🔧 <Tool-Name> …"-Statuszeile über on_partial gesendet (dokumentiertes
    Anthropic-Format, aber NICHT live gegen Claude Code CLI verifiziert). Jeder
    andere, noch unbekannte stream_event-Typ wird geloggt statt stillschweigend
    verworfen — falls Claude Code CLI ein eigenes Tool-Ergebnis-Event hat, taucht es
    dort auf.
    """
    # streaming=True: _build_agent_command hängt selbst die passenden Flags an
    # (--output-format stream-json --verbose --include-partial-messages) und filtert
    # eine ggf. noch gespeicherte alte "--output-format json"-Konfiguration heraus —
    # siehe Modul-Kommentar bei _CLAUDE_MANAGED_FLAGS_WITH_VALUE.
    built = _build_agent_command(cfg, prompt, system_prompt, files, session_id_override,
                                  streaming=True)
    if built is None:
        return None, None
    cmd, env, cwd, timeout, _is_hermes = built

    answer_field     = cfg.get('cli_answer_output_field', 'result')
    session_id_field = cfg.get('cli_session_id_output_field', '')

    log.info(f'Calling CLI agent (streaming): {cmd[0]} (timeout={timeout}s, cwd={cwd})')
    log.info(f'CLI args: {cmd[1:]}')

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, env=env, cwd=cwd, bufsize=1,
        )
    except Exception as e:
        log.error(f'streaming Popen failed: {e}')
        return None, None

    # Harte Zeitgrenze: ein Watchdog killt den Prozess nach timeout Sekunden. Das
    # blockierende Lesen von stdout endet dann per EOF (kein Deadlock bei stummem CLI).
    timed_out = {'v': False}
    def _kill_on_timeout():
        timed_out['v'] = True
        try:
            proc.kill()
        except Exception:
            pass
    watchdog = threading.Timer(timeout, _kill_on_timeout)
    watchdog.start()

    accumulated   = ''
    final_answer  = None
    extracted_sid = None
    last_post     = 0.0
    last_posted   = None

    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                # stream-json ist zeilenweise valides JSON; eine kaputte Zeile
                # überspringen wir (kosmetisch), statt den Lauf abzubrechen.
                continue

            # Finale Ergebniszeile: exakt wie im nicht-streamenden Pfad auswerten.
            if isinstance(evt, dict) and evt.get('type') == 'result':
                if session_id_field:
                    sid = json_get(evt, session_id_field) or ''
                    if sid:
                        extracted_sid = str(sid)
                final_answer = (json_get(evt, answer_field) or '').strip()
                continue

            # Zwischentext: NUR die 'stream_event'/content_block_delta/text_delta-Zeilen
            # tragen tatsächlich wachsenden Text (siehe Docstring). Komplette
            # 'assistant'-Messages werden hier bewusst NICHT zusätzlich ausgewertet —
            # das würde denselben Text doppelt anhängen, da er bereits Delta für Delta
            # hereinkam.
            if not isinstance(evt, dict) or evt.get('type') != 'stream_event':
                continue
            inner = evt.get('event') or {}
            if not isinstance(inner, dict):
                continue
            inner_type = inner.get('type')

            # Tool-Nutzung ankündigen (2026-07-26, analog zur Hermes-Tool-Progress-
            # Anzeige): content_block_start mit content_block.type=="tool_use" ist
            # dokumentiertes Anthropic-Streaming-Format und enthält den Tool-Namen
            # direkt. NICHT live gegen Claude Code CLI verifiziert (die Doku gilt für
            # die rohe Messages-API — Claude Code könnte zusätzlich wrappen), daher
            # defensiv geprüft und ohne Crash-Risiko bei abweichendem Format.
            if inner_type == 'content_block_start':
                block = inner.get('content_block') or {}
                if isinstance(block, dict) and block.get('type') == 'tool_use' and on_partial:
                    tool_name = block.get('name') or 'Tool'
                    status_line = f'🔧 {tool_name} …'
                    on_partial(status_line)
                    last_posted = status_line
                continue

            if inner_type != 'content_block_delta':
                # Bekannte, aber für uns uninteressante Strukturereignisse still
                # überspringen; alles WEITERE unbekannte loggen statt zu raten —
                # falls Claude Code CLI ein eigenes Tool-Ergebnis-Event hat, taucht
                # es hier auf und kann beim nächsten Live-Test ausgewertet werden.
                if inner_type not in ('message_start', 'message_delta', 'message_stop',
                                      'content_block_stop'):
                    log.info(f'Claude stream_event unbekannter Typ (Format-Diagnose): {inner_type}')
                continue

            delta = inner.get('delta') or {}
            if not isinstance(delta, dict) or delta.get('type') != 'text_delta':
                continue
            piece = delta.get('text') or ''
            if piece:
                accumulated += piece
                now = time.monotonic()
                if on_partial and (now - last_post) >= 1.5:
                    on_partial(accumulated)
                    last_post   = now
                    last_posted = accumulated

        proc.wait()
    except Exception as e:
        log.error(f'streaming read failed: {e}')
        try:
            proc.kill()
        except Exception:
            pass
        return None, None
    finally:
        watchdog.cancel()

    if timed_out['v']:
        log.error(f'streaming CLI timed out after {timeout}s')
        return None, None

    if proc.returncode not in (0, None):
        log.error(f'streaming CLI exited {proc.returncode}')
        return None, None

    if not final_answer:
        # Kein Ergebnis-Event → als Fehler behandeln, Aufrufer fällt zurück.
        log.error('streaming CLI produced no result line')
        return None, None

    if extracted_sid:
        store_session_id(extracted_sid)

    # Vollständigen Text ein letztes Mal posten (nahtloser Übergang zur put_answer,
    # die gleich darauf denselben Text final speichert).
    if on_partial and accumulated and accumulated != last_posted:
        on_partial(accumulated)

    log.info(f'CLI answered via streaming ({len(final_answer)} chars)')
    return final_answer, extracted_sid


def call_agent_hermes_streaming(cfg, server_cfg, prompt, system_prompt='', files=None,
                                session_id_override=None, on_partial=None, reasoning=None):
    """Streaming über Hermes' eigenen OpenAI-kompatiblen API-Server (siehe
    _hermes_api_server_ready) — komplett anderer Weg als bei Claude: kein
    CLI-Subprozess, sondern ein einzelner HTTP-Call an /v1/chat/completions mit
    stream=true.

    files: wie bei call_agent_cli — der API-Server kennt kein natives Datei-
    Attachment (nur Text-Messages), daher wird derselbe Text-Hinweis wie beim
    CLI-Fallback vor den Prompt gesetzt (siehe _file_note_prefix). Ehemals
    wurde bei Anhang komplett auf den CLI-Pfad ausgewichen — seit dieser
    Ergänzung kann auch der Streaming-Pfad Anhänge verarbeiten.

    reasoning: persistenter Override (none|minimal|low|medium|high|xhigh|max|
    ultra), 'auto'/None = kein Override. Laut Doku (docs.hermes-agent.
    nousresearch.com/docs/user-guide/features/api-server, "Per-request model
    selection") akzeptiert der Endpunkt ein "model_options": {"reasoning_effort":
    "..."}-Feld — NICHT live gegen eine echte Instanz mit stream=true verifiziert
    (Stand 2026-08-22). Bewusst kein zusätzlicher Korrektheits-Check: geht der
    Server mit dem Feld schief oder ignoriert es, liefert er trotzdem eine
    Antwort zurück, die dann ungeprüft übernommen wird — anders als bei einem
    harten Fehler (Exception/leerer Text unten), der wie gehabt auf den
    CLI-Pfad (mit /reasoning-Vorab-Aufruf, siehe _apply_hermes_reasoning)
    zurückfällt.

    Live verifiziert 2026-07-26 gegen eine echte Instanz (Kalender-MCP-Nutzung):
    OpenAI-Chat-Completions-SSE-Format ("data: {...}"-Frames, choices[0].delta.
    content, abschließend "data: [DONE]") bestätigt, ebenso der
    X-Hermes-Session-Id-Response-Header für Session-Kontinuität (Log zeigte
    "Session ID stored: api-..."). Zusätzlich bestätigt: Tool-Nutzung sendet
    "choices"-lose Events {"tool","emoji"?,"label"?,"toolCallId","status":
    "running"|"completed"} — nur "running" wird als kosmetischer Fortschritt
    gezeigt (siehe unten im Code), "completed" käme oft <1s später und würde nur
    flackern. NICHT verifiziert: ob eine native system-Rolle wirklich beachtet
    wird (kein Log-Beweis dafür/dagegen).

    Bei JEDEM Fehler (Verbindung, HTTP-Fehlercode, kaputtes/unerwartetes Format,
    kein Text erhalten) gibt diese Funktion (None, None) zurück — der Aufrufer
    (process_wakeup) fällt dann auf den bewährten CLI-Pfad (call_agent_cli) zurück,
    exakt wie beim Claude-Streaming.

    Rückgabe: (answer, session_id) — answer=None bei Fehler.
    """
    resume_id = session_id_override or _current_session_id

    file_note = _file_note_prefix(files)
    if file_note:
        prompt = f'{file_note}\n\n{prompt}' if prompt else file_note

    messages = []
    if system_prompt:
        # Angenommen: der Server unterstützt eine native system-Rolle (OpenAI-
        # kompatibel) — anders als der CLI-Pfad, der mangels --system-prompt-Flag
        # den Voranstellen-Trick braucht (_frame_one_time_system_prompt).
        messages.append({'role': 'system', 'content': system_prompt})
    messages.append({'role': 'user', 'content': prompt})

    url = f"http://{server_cfg['host']}:{server_cfg['port']}/v1/chat/completions"
    headers = {'Content-Type': 'application/json'}
    if server_cfg.get('key'):
        headers['Authorization'] = f"Bearer {server_cfg['key']}"
    if resume_id:
        headers['X-Hermes-Session-Id'] = resume_id

    timeout = cfg.get('cli_timeout', 600)
    accumulated = ''
    last_post = 0.0
    last_posted = None

    body = {'messages': messages, 'stream': True}
    if reasoning and reasoning != 'auto':
        body['model_options'] = {'reasoning_effort': reasoning}

    log.info(f'Calling Hermes API server (streaming): {url}'
             + (f' [reasoning_effort={reasoning}]' if reasoning and reasoning != 'auto' else ''))

    try:
        with requests.post(url, headers=headers,
                            json=body,
                            stream=True, timeout=timeout) as resp:
            if resp.status_code != 200:
                log.error(f'Hermes API server returned HTTP {resp.status_code}')
                return None, None

            # Angenommen: eine (neue oder fortgesetzte) Session-ID kommt als
            # Response-Header zurück. Falls anders übertragen, bleibt es schlicht
            # bei resume_id (bzw. leer bei neuer Session) — kein Crash, nur keine
            # Session-Kontinuität für diesen einen Call.
            session_id = resp.headers.get('X-Hermes-Session-Id') or resume_id

            for line in resp.iter_lines(decode_unicode=True):
                if not line or not line.startswith('data:'):
                    continue
                payload = line[len('data:'):].strip()
                if payload == '[DONE]':
                    break
                try:
                    evt = json.loads(payload)
                except json.JSONDecodeError:
                    continue

                choices = evt.get('choices') or []
                if not choices:
                    # Tool-Progress-Event — live verifiziert 2026-07-26 gegen eine
                    # echte Instanz (Kalender-MCP-Aufruf): {"tool": "...",
                    # "emoji"?: "...", "label"?: "...", "toolCallId": "...",
                    # "status": "running"|"completed"}. Nur "running" anzeigen (nicht
                    # "completed" zusätzlich — die Paare kommen oft <1s auseinander,
                    # das würde nur flackern). Bewusst NICHT in "accumulated"
                    # aufgenommen — das ist kein Teil der finalen Antwort.
                    if evt.get('tool') and evt.get('status') == 'running':
                        if on_partial:
                            emoji = evt.get('emoji', '')
                            label = evt.get('label') or evt.get('tool')
                            status_line = f'{emoji} {label} …'.strip()
                            on_partial(status_line)
                            last_posted = status_line
                    else:
                        # Alles andere Unbekannte weiter loggen statt zu raten.
                        log.info(f'Hermes SSE event ohne choices (Format-Diagnose): {payload[:300]}')
                    continue
                piece = (choices[0].get('delta') or {}).get('content') or ''
                if piece:
                    accumulated += piece
                    now = time.monotonic()
                    if on_partial and (now - last_post) >= 1.5:
                        on_partial(accumulated)
                        last_post   = now
                        last_posted = accumulated
    except Exception as e:
        log.warning(f'Hermes API server streaming failed (falling back to CLI): {e}')
        return None, None

    if not accumulated:
        log.warning('Hermes API server returned no content — falling back to CLI.')
        return None, None

    if session_id:
        store_session_id(session_id)

    # Entgegen der Nous-Research-Doku (API-Server soll Reasoning NICHT im Response
    # einschließen, siehe call_agent_hermes_streaming-Docstring) hat sich in der
    # Praxis gezeigt, dass die Reasoning-Box trotzdem als normaler content-Delta-
    # Text durchgereicht wird, wenn ein Override aktiv ist — dieselbe Aufbereitung
    # wie im CLI-Pfad (_parse_hermes_output) daher auch hier anwenden.
    accumulated = _format_hermes_reasoning_block(accumulated)

    if on_partial and accumulated != last_posted:
        on_partial(accumulated)

    log.info(f'Hermes API server answered via streaming ({len(accumulated)} chars)')
    return accumulated, session_id


def call_agent_openclaw_streaming(cfg, server_cfg, prompt, system_prompt='', files=None,
                                  session_id_override=None, on_partial=None):
    """Streaming über OpenClaws eigenen Gateway-OpenAI-kompatiblen
    Chat-Completions-Endpunkt (siehe _openclaw_streaming_ready). Wire-Format laut
    offizieller Doku (docs.openclaw.ai/gateway/openai-http-api, 2026-07 recherchiert):
    Standard-OpenAI-SSE ("data: {...}", choices[0].delta.content, "data: [DONE]").

    files: siehe call_agent_hermes_streaming — derselbe Text-Hinweis-Trick, da
    auch dieser Gateway-Endpunkt kein natives Datei-Attachment kennt.

    OFFENE FRAGE (2026-07-26, noch NICHT live verifiziert): Tool-Aufrufe kommen laut
    Doku als delta.tool_calls-Chunks, gefolgt von einem Chunk mit
    finish_reason=="tool_calls" — das Standard-OpenAI-Function-Calling-Protokoll, bei
    dem normalerweise der CALLER das Tool ausführen und das Ergebnis in einem
    Folge-Request zurückmelden muss. Unklar: löst OpenClaw seine eigenen
    (MCP-)Tools intern selbst auf (Stream läuft weiter bis zur echten Antwort, wie
    bei Hermes) — oder bricht der Stream danach wirklich ab? Absichtlich KEIN
    Sonderfall-Code dafür: falls Letzteres zutrifft, bleibt "accumulated" leer (keine
    echten Text-Deltas empfangen) → das bestehende "leere Antwort = Fehler"-Verhalten
    unten löst automatisch den CLI-Fallback aus, ganz ohne Extra-Logik. Jeder
    finish_reason wird zusätzlich geloggt, damit sich diese Frage aus einem echten
    Testlauf beantworten lässt (siehe Diagnose-Logging unten).

    Session-Kontinuität: eigener Header x-openclaw-session-key (dokumentiert) — WIR
    bestimmen den Wert (neu generiert, falls keine bestehende Session), anders als
    bei Hermes, wo der Server eine ID zurückgibt.

    Rückgabe: (answer, session_id) — answer=None bei Fehler.
    """
    resume_id = session_id_override or _current_session_id
    if not resume_id:
        import uuid as _uuid
        resume_id = str(_uuid.uuid4())

    file_note = _file_note_prefix(files)
    if file_note:
        prompt = f'{file_note}\n\n{prompt}' if prompt else file_note

    messages = []
    if system_prompt:
        messages.append({'role': 'system', 'content': system_prompt})
    messages.append({'role': 'user', 'content': prompt})

    url = f"http://{server_cfg['host']}:{server_cfg['port']}/v1/chat/completions"
    headers = {
        'Content-Type':           'application/json',
        'Authorization':          f"Bearer {server_cfg['token']}",
        'x-openclaw-session-key': resume_id,
    }
    timeout = cfg.get('cli_timeout', 600)

    accumulated  = ''
    last_post    = 0.0
    last_posted  = None
    announced_tool_indexes = set()

    log.info(f'Calling OpenClaw gateway (streaming): {url}')

    try:
        with requests.post(url, headers=headers,
                            json={'model': 'openclaw', 'messages': messages, 'stream': True},
                            stream=True, timeout=timeout) as resp:
            if resp.status_code != 200:
                log.error(f'OpenClaw gateway returned HTTP {resp.status_code}: {resp.text[:300]}')
                return None, None

            for line in resp.iter_lines(decode_unicode=True):
                if not line or not line.startswith('data:'):
                    continue
                payload = line[len('data:'):].strip()
                if payload == '[DONE]':
                    break
                try:
                    evt = json.loads(payload)
                except json.JSONDecodeError:
                    continue

                choices = evt.get('choices') or []
                if not choices:
                    log.info(f'OpenClaw SSE event ohne choices (Format-Diagnose): {payload[:300]}')
                    continue

                choice        = choices[0]
                delta         = choice.get('delta') or {}
                finish_reason = choice.get('finish_reason')
                if finish_reason:
                    # Diagnose (2026-07-26): zeigt genau, ob nach "tool_calls" noch
                    # ein weiterer Chunk mit "stop" + echtem Text kommt, oder ob das
                    # bereits das Ende ist — beantwortet die offene Frage oben.
                    log.info(f'OpenClaw stream finish_reason (Format-Diagnose): {finish_reason}')

                # Tool-Aufruf ankündigen (Standard-OpenAI-Function-Calling-Delta) —
                # nur beim ERSTEN Chunk pro Tool-Call-Index (function.name steht nur
                # dort, spätere Chunks tragen nur noch Argument-Fragmente).
                tool_calls = delta.get('tool_calls')
                if tool_calls:
                    # Diagnose (2026-07-26): erster Live-Test zeigte trotz sichtbarer
                    # MCP-Nutzung (finish_reason blieb bei "stop", kein "tool_calls")
                    # keine Statuszeile — kompletten Chunk loggen, um zu sehen ob
                    # überhaupt tool_calls-Deltas ankommen und wie sie genau aussehen.
                    log.info(f'OpenClaw delta.tool_calls (Format-Diagnose): {tool_calls}')
                    for tc in tool_calls:
                        idx  = tc.get('index', 0)
                        name = (tc.get('function') or {}).get('name')
                        if name and idx not in announced_tool_indexes:
                            announced_tool_indexes.add(idx)
                            if on_partial:
                                status_line = f'🔧 {name} …'
                                on_partial(status_line)
                                last_posted = status_line
                elif delta and not delta.get('content'):
                    # Diagnose: nicht-leeres Delta ohne content UND ohne tool_calls
                    # (z.B. reine role-Ankündigung ist normal, aber evtl. steckt hier
                    # OpenClaws Tool-Signal in einer ganz anderen Form).
                    log.info(f'OpenClaw delta ohne content/tool_calls (Format-Diagnose): {delta}')

                piece = delta.get('content') or ''
                if piece:
                    accumulated += piece
                    now = time.monotonic()
                    if on_partial and (now - last_post) >= 1.5:
                        on_partial(accumulated)
                        last_post   = now
                        last_posted = accumulated
    except Exception as e:
        log.warning(f'OpenClaw gateway streaming failed (falling back to CLI): {e}')
        return None, None

    if not accumulated:
        log.warning('OpenClaw gateway returned no content — falling back to CLI.')
        return None, None

    store_session_id(resume_id)

    if on_partial and accumulated != last_posted:
        on_partial(accumulated)

    log.info(f'OpenClaw gateway answered via streaming ({len(accumulated)} chars)')
    return accumulated, resume_id


def _safe_attachment_name(name, job_id, index=0):
    """Bereinigt den Dateinamen bridge-seitig auf einen sicheren Basisnamen (keine
    Pfad-Anteile → keine Traversal), behält die Endung. Job-ID + Zeitstempel voran,
    damit wiederholte Uploads für denselben Job sich nicht überschreiben. index
    zusätzlich davor, da mehrere Anhänge desselben Jobs (Mehrfachauswahl) denselben
    Zeitstempel (Sekundenauflösung) UND denselben Originalnamen haben können (z.B.
    zwei Fotos, beide "foto.jpg") — ohne index würden sie sich überschreiben."""
    base = os.path.basename(name or '').strip()
    base = re.sub(r'[^\w.\- ]+', '_', base)
    if not base or base in ('.', '..'):
        base = 'anhang'
    return f'ios_att_{job_id}_{time.strftime("%Y%m%d_%H%M%S")}_{index}_{base}'


def download_attachment(cfg, job_id, attachment_token, attachment_name, index=0):
    """Lädt den per Chunked-Upload zusammengesetzten Datei-Anhang eines Jobs vom
    Server (download_attachment.php, token_b-Auth) und legt ihn unter session_files/
    ab. Gibt den lokalen Pfad zurück oder None bei Fehler. Token-B rotiert hier NICHT
    (Download-Response ist binär) — der get_job/put_answer-Zyklus rotiert ohnehin.
    index: Position innerhalb der attachments-Liste dieses Jobs (siehe
    _safe_attachment_name) — bei mehreren Anhängen nötig, um Namenskollisionen zu
    vermeiden, sonst unbenutzt (Default 0)."""
    try:
        r = requests.post(
            cfg['server_url'] + '/download_attachment.php',
            json={'token_b': cfg['token_b'], 'attachment_token': attachment_token},
            timeout=60,
        )
        if r.status_code != 200:
            log.error(f'download_attachment: HTTP {r.status_code} für job #{job_id}: {r.text[:200]}')
            return None
        dest_dir = REPO_DIR / 'session_files'
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / _safe_attachment_name(attachment_name, job_id, index)
        dest.write_bytes(r.content)
        log.info(f'Attachment downloaded for job #{job_id}: {dest} ({len(r.content)} bytes)')
        return str(dest)
    except Exception as e:
        log.error(f'download_attachment failed for job #{job_id}: {e}')
        return None


# ─────────────────────────────────────────────
# Session-Datei aus dem Verlauf anzeigen (Umkehrung von download_attachment:
# hier hat die Bridge die Datei, die App will sie sehen)
# ─────────────────────────────────────────────

SESSION_FILES_DIR   = (REPO_DIR / 'session_files').resolve()
SF_UPLOAD_CHUNK      = 2 * 1024 * 1024   # 2 MB, sicher unter dem 3-MB-Chunk-Limit von put_session_file.php
SF_MAX_UPLOAD_BYTES  = 20 * 1024 * 1024  # muss zu SF_MAX_TOTAL_BYTES in put_session_file.php passen


def _resolve_session_file_path(file_path):
    """Prüft, dass der von der App gemeldete Pfad TATSÄCHLICH innerhalb von
    session_files/ liegt (dorthin lädt ausschließlich download_attachment()
    Dateien herunter) — verhindert beliebigen Datei-Lesezugriff über einen
    manipulierten/erratenen Pfad. Gibt den aufgelösten Path zurück oder None."""
    try:
        resolved = Path(file_path).expanduser().resolve()
        resolved.relative_to(SESSION_FILES_DIR)
    except (ValueError, OSError):
        return None
    if not resolved.is_file():
        return None
    return resolved


def _report_session_file_error(cfg, message):
    try:
        r = requests.post(
            cfg['server_url'] + '/put_session_file_error.php',
            json={'token_b': cfg['token_b'], 'error': message},
            timeout=20,
        )
        data = r.json()
        if 'token_b_new' in data:
            cfg['token_b'] = data['token_b_new']
            save_config(cfg)
        log.warning(f'send_session_file: {message}')
    except Exception as e:
        log.error(f'_report_session_file_error: post failed: {e}')


def send_session_file(cfg, file_path):
    """Wird via MQTT action=send-session-file ausgelöst (App hat im
    Nachrichtentext einen session_files-Pfad erkannt und angetippt). Lädt die
    Datei in Chunks zu put_session_file.php hoch (spiegelbildlich zu
    upload_chunk.php/download_attachment, nur Bridge → Server statt App →
    Server); bei jedem Validierungs-/Übertragungsfehler wird stattdessen
    put_session_file_error.php gemeldet, damit die App nicht endlos pollt."""
    if not file_path:
        _report_session_file_error(cfg, 'Kein Dateipfad übermittelt')
        return

    resolved = _resolve_session_file_path(file_path)
    if resolved is None:
        _report_session_file_error(cfg, 'Datei nicht gefunden oder außerhalb von session_files/')
        return

    size = resolved.stat().st_size
    if size > SF_MAX_UPLOAD_BYTES:
        _report_session_file_error(cfg, f'Datei zu groß ({size} bytes, Limit {SF_MAX_UPLOAD_BYTES})')
        return

    file_name = resolved.name
    total     = max(1, (size + SF_UPLOAD_CHUNK - 1) // SF_UPLOAD_CHUNK)

    try:
        with open(resolved, 'rb') as fh:
            for index in range(total):
                chunk = fh.read(SF_UPLOAD_CHUNK)
                r = requests.post(
                    cfg['server_url'] + '/put_session_file.php',
                    params={'index': index, 'total': total, 'file_name': file_name},
                    headers={'X-AC-Token-B': cfg['token_b'], 'Content-Type': 'application/octet-stream'},
                    data=chunk,
                    timeout=60,
                )
                if r.status_code != 200:
                    log.error(f'send_session_file: HTTP {r.status_code} bei Chunk {index}/{total}: {r.text[:200]}')
                    _report_session_file_error(cfg, 'Übertragung fehlgeschlagen')
                    return
                data = r.json()
                if data.get('complete'):
                    if 'token_b_new' in data:
                        cfg['token_b'] = data['token_b_new']
                        save_config(cfg)
                    log.info(f'send_session_file: {file_name} ({size} bytes) hochgeladen')
    except Exception as e:
        log.error(f'send_session_file failed for {resolved}: {e}')
        _report_session_file_error(cfg, 'Übertragung fehlgeschlagen')


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
    # Mehrere Anhänge (neuer Server): Liste [{"token":..., "name":...}, ...].
    # Fällt auf die alten Einzelfelder zurück, falls "attachments" fehlt (älterer
    # Server, der das Feld noch nicht kennt) — symmetrisch zum Fallback, den die
    # submit-Action serverseitig für eine alte App-Version macht.
    attachments = job.get('attachments') or []
    if not attachments:
        legacy_token = job.get('attachment_token', '')
        legacy_name  = job.get('attachment_name', '')
        if legacy_token:
            attachments = [{'token': legacy_token, 'name': legacy_name}]
    # App-gesteuertes Fortsetzen eines bestimmten (u.U. alten) Gesprächs: hat Vorrang
    # vor der RAM-gehaltenen Session. Leer bei Legacy-Jobs (Alexa/Siri/alte App) oder
    # wenn reset_history bereits True ist — dann verhält sich alles wie bisher.
    job_session_id = job.get('session_id') or None
    # Streaming: nur wenn die App es angefordert hat UND ein streaming-fähiger Pfad
    # existiert — Claude (CLI, sauberes NDJSON) ODER Hermes/OpenClaw MIT aktiviertem
    # und erreichbarem eigenem Gateway (2026-07-25/26 ergänzt). Die Hermes-/OpenClaw-
    # Checks laufen nur, wenn wants_stream überhaupt angefragt UND das jeweilige
    # Binary aktiv ist — kein unnötiger Config-Read/Live-Probe bei jedem Job.
    wants_stream_requested = bool(job.get('wants_stream'))
    is_claude_cli    = _is_claude_binary(_cli_binary(cfg))
    is_hermes_cli    = _is_hermes_binary(_cli_binary(cfg))
    is_openclaw_cli  = _is_openclaw_binary(_cli_binary(cfg))
    # Reasoning-Level, persistent am Profil (ConfigView.swift/settings.php UND
    # ModelQuickSettingsSheet.swift/index.php binden an denselben Wert, wie
    # hermes_model) — 'auto' = kein Override. Bei aktivem Override wird der
    # Gateway-Streaming-Pfad TROTZDEM probiert (call_agent_hermes_streaming
    # hängt dann "model_options": {"reasoning_effort": ...} an den Request —
    # laut Doku vom Server unterstützt, siehe dortigen Kommentar zum
    # Verifikationsstand). Schlägt der Streaming-Call fehl (Exception/HTTP-
    # Fehler/leerer Text), greift wie gehabt der CLI-Fallback unten mit
    # /reasoning-Vorab-Aufruf (_apply_hermes_reasoning) — das bleibt der
    # garantiert korrekte Pfad, falls model_options doch nicht wirkt.
    hermes_reasoning = cfg.get('hermes_reasoning') or 'auto'
    hermes_api_cfg   = (_hermes_api_server_ready(cfg)
                        if (wants_stream_requested and is_hermes_cli)
                        else None)
    openclaw_api_cfg = _openclaw_streaming_ready(cfg) if (wants_stream_requested and is_openclaw_cli) else None
    wants_stream     = wants_stream_requested and (
        is_claude_cli or hermes_api_cfg is not None or openclaw_api_cfg is not None
    )

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

    # Chunked-Upload-Anhänge (neue App, ggf. mehrere): jede Datei separat vom
    # Server holen (umgeht das ~3 MB Webserver-Upload-Limit; der Download ist
    # eine Response, unbegrenzt).
    for index, att in enumerate(attachments):
        token = att.get('token', '')
        if not token:
            continue
        f = download_attachment(cfg, job_id, token, att.get('name', ''), index)
        if f:
            downloaded_files.append(f)

    if wants_stream:
        # Streaming-Pfad: Zwischentext gedrosselt an put_partial.php schicken. Bei
        # jedem Fehlschlag (kein Ergebnis, CLI-Fehler, Timeout) auf den bewährten
        # nicht-streamenden Pfad zurückfallen, damit der Nutzer trotzdem eine Antwort
        # bekommt — Streaming ist reines Zusatz-Komfort, kein kritischer Pfad.
        if is_claude_cli:
            log.info(f'Job #{job_id}: streaming enabled (Claude)')
            answer, new_session_id = call_agent_cli_streaming(
                cfg, prompt, system_prompt, files=downloaded_files or None,
                session_id_override=job_session_id,
                on_partial=lambda text: put_partial(cfg, job_id, text),
            )
        elif hermes_api_cfg is not None:
            log.info(f'Job #{job_id}: streaming enabled (Hermes API server)')
            answer, new_session_id = call_agent_hermes_streaming(
                cfg, hermes_api_cfg, prompt, system_prompt, files=downloaded_files or None,
                session_id_override=job_session_id,
                on_partial=lambda text: put_partial(cfg, job_id, text),
                reasoning=hermes_reasoning,
            )
        else:
            log.info(f'Job #{job_id}: streaming enabled (OpenClaw gateway)')
            answer, new_session_id = call_agent_openclaw_streaming(
                cfg, openclaw_api_cfg, prompt, system_prompt, files=downloaded_files or None,
                session_id_override=job_session_id,
                on_partial=lambda text: put_partial(cfg, job_id, text),
            )
        if answer is None:
            log.warning(f'Job #{job_id}: streaming failed — falling back to non-streaming run.')
            answer, new_session_id = call_agent_cli(
                cfg, prompt, system_prompt, files=downloaded_files or None,
                session_id_override=job_session_id, hermes_reasoning=hermes_reasoning,
            )
    else:
        answer, new_session_id = call_agent_cli(
            cfg, prompt, system_prompt, files=downloaded_files or None,
            session_id_override=job_session_id, hermes_reasoning=hermes_reasoning,
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


# Openclaw wrapt eingehende Nachrichten aus manchen Kanälen (z.B. Telegram) mit
# Metadaten-Blöcken und einer wiederholten, chronologischen Kontext-Historie
# (inkl. bereits gezeigter Agenten-Antworten) für das Modell. Ohne Bereinigung
# würde der Verlauf-Tab als Titel/erste Nachricht diesen ganzen Wrapper zeigen
# statt der eigentlichen neuen Nutzer-Nachricht. Beispielstruktur:
#
#   Conversation info (untrusted metadata):
#   ```json
#   { "chat_id": "...", ... }
#   ```
#
#   Sender (untrusted metadata):
#   ```json
#   { "label": "...", ... }
#   ```
#
#   Conversation context (untrusted, chronological, selected for current message):
#   #3275 Sat 2026-06-13 20:38 GMT+2 Ingo Keutgen: <alte Nachricht>
#   #3277 ... OpenClaw: <alte Antwort>
#
#   <eigentliche neue Nachricht>
_OPENCLAW_CONTEXT_MARKER = (
    'Conversation context (untrusted, chronological, selected for current message):'
)
_OPENCLAW_METADATA_BLOCK_RE = re.compile(
    r'(?:Conversation info|Sender) \(untrusted metadata\):\s*```json.*?```\s*',
    re.DOTALL
)

# Standard-System-Prompts (siehe ios_app_endpoint.php $sp_chat/$sp_siri bzw.
# L10n.defaultSystemPromptChat/Siri in der App).
#
# NUR noch für ALTBESTAND nötig: Sessions, die vor der Einrahmung (siehe
# _frame_one_time_system_prompt) entstanden sind, enthalten den System-Prompt roh
# vorangestellt. Dort hilft nur exaktes Matchen — individuell angepasste Prompts
# werden in solchen Alt-Sessions nicht erkannt und bleiben stehen. Neue Sessions
# sind eingerahmt und werden über die eindeutigen Marker sauber entfernt,
# unabhängig davon, ob der Prompt angepasst wurde.
_KNOWN_DEFAULT_SYSTEM_PROMPTS = (
    "Du bist ein hilfreicher Assistent. Deine Antworten werden per Text-Nachricht übermittelt.\nHalte dich an die folgenden Regeln:\n\n## Formatierung\nNutze gerne für die bessere Lesbarkeit in den Antworten übliche Markdown-Auszeichnungen.",
    "You are a helpful assistant. Your answers are delivered via text message.\nFollow these rules:\n\n## Formatting\nFeel free to use common Markdown formatting to improve readability.",
    "Du bist ein hilfreicher Sprachassistent. Deine Antworten werden per Text-to-Speech vorgelesen. Halte dich strikt an diese Regeln:\n\n## Antwortstil\n- Antworte in natürlicher, gesprochener Sprache — als würdest du mit jemandem reden, nicht schreiben.\n- Halte Antworten KURZ. Maximal 2–3 Sätze, außer es wird ausdrücklich mehr Detail verlangt.\n- Verwende kein Markdown: keine Aufzählungszeichen, keine Fettschrift, keine Überschriften, keine Listen, keine Codeblöcke.\n- Verwende keine Abkürzungen, die beim Vorlesen seltsam klingen. Schreibe \"zum Beispiel\" statt \"z.B.\".\n- Zahlen und Einheiten ausschreiben: \"drei Kilometer\" statt \"3 km\".\n- Keine Klammern, Schrägstriche oder Sonderzeichen.\n\n## Gesprächsstil\n- Komm direkt zum Punkt. Erst die Antwort, dann — wenn nötig — kurzer Kontext.\n- Bei mehrdeutigen Fragen: eine vernünftige Annahme treffen und kurz benennen.\n- Maximal eine Rückfrage stellen, und nur wenn sie wirklich nötig ist.\n- Aktionen mit kurzen, natürlichen Sätzen bestätigen: \"Erledigt, der Timer läuft.\" — nicht \"Ich habe die angeforderte Aktion erfolgreich ausgeführt.\"\n\n## Sprache\n- Immer auf Deutsch antworten, unabhängig von der Eingabe.\n- Warmer, gesprächiger Ton — nicht förmlich, nicht steif.\n\n## Grenzen\n- Wenn etwas nicht möglich ist: einen kurzen Satz, und wenn möglich eine Alternative anbieten.\n\n## Quellenangaben\n- Gebe die Quellen nur als Überschrift an\n- Gebe detaillierte Quellen wie URLs nur auf Rückfragen an",
    "You are a helpful voice assistant. Your answers will be read aloud via text-to-speech. Follow these rules strictly:\n\n## Response style\n- Answer in natural, spoken language — as if talking to someone, not writing.\n- Keep answers SHORT. Maximum 2–3 sentences, unless more detail is explicitly requested.\n- Use no Markdown: no bullet points, no bold, no headings, no lists, no code blocks.\n- Avoid abbreviations that sound strange when read aloud. Write \"for example\" instead of \"e.g.\".\n- Write out numbers and units: \"three kilometers\" instead of \"3 km\".\n- No parentheses, slashes, or special characters.\n\n## Conversation style\n- Get straight to the point. Answer first, then — if needed — brief context.\n- For ambiguous questions: make a reasonable assumption and briefly state it.\n- Ask at most one follow-up question, and only if truly necessary.\n- Confirm actions with short, natural sentences: \"Done, the timer is running.\" — not \"I have successfully executed the requested action.\"\n\n## Language\n- Always answer in English, regardless of the input.\n- Warm, conversational tone — not formal, not stiff.\n\n## Limits\n- If something is not possible: one short sentence, and if possible offer an alternative.\n\n## Sources\n- Mention sources as headings only\n- Provide detailed sources like URLs only when asked",
)


_OPENCLAW_LEADING_TIMESTAMP_RE = re.compile(r'^\[[^\[\]]{1,60}\]\s*')


def _strip_injected_system_prompt(text):
    """Entfernt einen von uns injizierten System-Prompt am Textanfang — für den
    Verlauf-Tab, damit dort die echte Nutzer-Nachricht steht und nicht der
    vorangestellte System-Prompt. Gilt für alle Backends ohne System-Prompt-Flag
    (Openclaw, Hermes).

    Zwei Formate: aktuell eingerahmt (Marker eindeutig, erkennt auch angepasste
    Prompts) und Altbestand ungerahmt (nur die Standard-Prompts erkennbar)."""
    stripped = _strip_one_time_frame(text)
    if stripped != text:
        return stripped
    for sp in _KNOWN_DEFAULT_SYSTEM_PROMPTS:
        prefix = sp + '\n\n'
        if text.startswith(prefix):
            return text[len(prefix):]
    return text


def _openclaw_strip_system_prompt(text):
    # Manche Kanäle stellen der ganzen Nachricht (System-Prompt inklusive) einen
    # Zeitstempel wie "[Sat 2026-06-13 20:52 GMT+2] " voran.
    lead_match = _OPENCLAW_LEADING_TIMESTAMP_RE.match(text)
    lead = lead_match.group(0) if lead_match else ''
    body = text[len(lead):]
    stripped = _strip_injected_system_prompt(body)
    return stripped if stripped != body else text


def _openclaw_extract_current_message(prompt_text):
    text = _openclaw_strip_system_prompt(prompt_text)
    idx = text.find(_OPENCLAW_CONTEXT_MARKER)
    if idx != -1:
        after = text[idx + len(_OPENCLAW_CONTEXT_MARKER):]
        lines = after.split('\n')
        last_context_line = -1
        for i, line in enumerate(lines):
            if line.strip().startswith('#'):
                last_context_line = i
        remainder = '\n'.join(lines[last_context_line + 1:]).strip()
        if remainder:
            return remainder
        # Nichts nach der letzten Kontext-Zeile gefunden — auf die verbleibenden
        # Metadaten-Blöcke unten zurückfallen, statt leer zurückzugeben.
        text = after

    return _OPENCLAW_METADATA_BLOCK_RE.sub('', text).strip()


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
                    raw = ((o.get('data') or {}).get('prompt') or '').strip()
                    text = _openclaw_extract_current_message(raw) if raw else ''
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
                    raw = ((o.get('data') or {}).get('prompt') or '').strip()
                    text = _openclaw_extract_current_message(raw) if raw else ''
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


# --- Hermes (Sessions liegen in SQLite, Zugriff über die hermes-CLI statt Dateien) ---

def _is_hermes_cfg(cfg):
    return _is_hermes_binary(_cli_binary(cfg))


def _hermes_run(cfg, args, timeout=60):
    """Führt `<hermes-binary> <args>` mit gesetztem HERMES_HOME (Profil-Home) aus.
    cli_command kann 'hermes chat' sein — für sessions/profile zählt nur das Binary
    (cmd[0]). Gibt stdout (str) zurück oder None bei Fehler."""
    binary = _cli_binary(cfg)
    if not binary:
        return None
    env = os.environ.copy()
    env.update(cfg.get('cli_env', {}))
    env['HERMES_HOME'] = _hermes_home_for(cfg.get('hermes_profile', ''))
    cwd = os.path.expanduser(cfg['cli_working_dir']) if cfg.get('cli_working_dir') else None
    try:
        r = subprocess.run([binary] + args, capture_output=True, text=True,
                           env=env, cwd=cwd, timeout=timeout)
    except Exception as e:
        log.error(f'hermes {args[:2]} failed: {e}')
        return None
    if r.returncode != 0:
        log.error(f'hermes {args[:2]} exited {r.returncode}: {r.stderr[:300]}')
        return None
    return r.stdout


def _hermes_sid_to_iso(sid):
    """Der Zeitstempel steckt im ID-Präfix (YYYYMMDD_HHMMSS, lokale Zeit) — präziser
    als die relative 'Last Active'-Spalte der Tabelle."""
    m = re.match(r'(\d{8})_(\d{6})', sid or '')
    if not m:
        return ''
    try:
        from datetime import datetime
        return datetime.strptime(m.group(1) + m.group(2), '%Y%m%d%H%M%S').astimezone().isoformat()
    except Exception:
        return ''


def _hermes_epoch_to_iso(ts):
    if not ts:
        return ''
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except Exception:
        return ''


_HERMES_SESSION_ID_RE = re.compile(r'\d{8}_\d{6}|api-[0-9a-fA-F]+')


def _parse_hermes_sessions_table(text):
    """Parst `hermes sessions list`. Spalten sind durch 2+ Leerzeichen getrennt
    (Titel selbst enthält nur einfache Leerzeichen) — robuster als Offset-Slicing.
    Die ID ist normalerweise im Format YYYYMMDD_HHMMSS…, der Titel steht ganz
    vorne. Sessions aus dem Hermes-API-Server (siehe call_agent_hermes_streaming)
    haben stattdessen eine `api-<hex>`-ID — ohne das zweite Regex-Alternativ
    wurden diese Zeilen hier stillschweigend verworfen und tauchten dadurch nie
    im Verlauf-Tab auf (live verifiziert 2026-07-27: Zeile war in `sessions list`
    vorhanden, nur der Parser hat sie rausgefiltert)."""
    lines = text.splitlines()
    header_idx = next((i for i, l in enumerate(lines)
                       if 'Title' in l and 'ID' in l and 'Last Active' in l), None)
    if header_idx is None:
        return []
    entries = []
    for line in lines[header_idx + 1:]:
        s = line.strip()
        if not s or set(s) <= set('─-—│| '):  # leer oder Trennlinie (inkl. Leerzeichen)
            continue
        parts = re.split(r'\s{2,}', s)
        sid = next((p.split()[0] for p in parts if _HERMES_SESSION_ID_RE.match(p)), '')
        if not sid:
            continue
        title = parts[0] if parts and not _HERMES_SESSION_ID_RE.match(parts[0]) else ''
        entries.append({
            'session_id':    sid,
            'title':         title,  # kann leer/Platzhalter sein — siehe _hermes_history_list
            'updated_at':    _hermes_sid_to_iso(sid),
            'message_count': 0,
        })
    return entries


_HERMES_PLACEHOLDER_TITLE_RE = re.compile(r'^[\s\-–—_]*$')


def _hermes_history_list(cfg):
    out = _hermes_run(cfg, ['sessions', 'list', '--limit', '200'], timeout=60)
    if out is None:
        return []
    entries = _parse_hermes_sessions_table(out)
    # Hermes zeigt für CLI-erzeugte One-Shot-Sessions oft nur einen Platzhalter
    # (z.B. "—") statt eines echten Titels — dessen eigene Titel-Generierung greift
    # anscheinend nur bei "richtigen" interaktiven Gesprächen. In dem Fall den
    # Anfang der ersten echten Nutzer-Nachricht als Titel nachladen. Ein Export pro
    # betroffener Session ist teuer (spürbar langsamer Verlauf-Tab bei vielen
    # Sessions) — vergangene Gespräche ändern sich aber nie wieder, deshalb wird
    # ein einmal aufgelöster Titel gecacht und nur für WIRKLICH neue Sessions
    # (noch nicht im Cache) erneut ein Export gemacht.
    cache = load_hermes_title_cache()
    cache_dirty = False
    for entry in entries:
        if not _HERMES_PLACEHOLDER_TITLE_RE.match(entry['title']):
            continue
        sid = entry['session_id']
        if sid in cache:
            entry['title'] = cache[sid]
            continue
        messages = _hermes_history_detail(cfg, sid)
        first_user = next((m['content'] for m in (messages or []) if m['role'] == 'user'), '')
        resolved = first_user[:60] if first_user else 'Gespräch'
        entry['title'] = resolved
        # 'Gespräch' (kein first_user gefunden) nicht cachen — könnte ein
        # vorübergehender Export-Fehler sein, beim nächsten Mal erneut versuchen.
        if first_user:
            cache[sid] = resolved
            cache_dirty = True
    if cache_dirty:
        save_hermes_title_cache(cache)
    return entries


def _hermes_history_detail(cfg, session_id):
    out = _hermes_run(cfg, ['sessions', 'export', '-', '--format', 'jsonl',
                            '--session-id', session_id], timeout=120)
    if out is None:
        return None
    messages = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        for m in obj.get('messages', []) if isinstance(obj, dict) else []:
            if not isinstance(m, dict):
                continue
            role = m.get('role')
            content = (m.get('content') or '').strip()
            ts = _hermes_epoch_to_iso(m.get('timestamp'))
            # Nur sichtbare Nutzer-/Agenten-Nachrichten. Raus: tool, session_meta,
            # leere assistant-Turns (nur tool_calls) und alle reasoning*-Felder.
            if role == 'user' and content:
                # Von der Bridge erzeugte Sessions haben den System-Prompt vorangestellt
                # (Hermes hat kein System-Prompt-Flag) — hier wieder entfernen, sonst
                # stünde er im Verlauf statt der eigentlichen Nutzer-Nachricht.
                content = _strip_injected_system_prompt(content).strip()
                if not content:
                    continue
                messages.append({'role': 'user', 'content': content, 'timestamp': ts})
            elif role == 'assistant' and content:
                messages.append({'role': 'agent', 'content': content, 'timestamp': ts})
    return messages


def _hermes_delete_session(cfg, session_id):
    """Löscht eine Hermes-Session headless. Rückgabe (deleted, errors)."""
    out = _hermes_run(cfg, ['sessions', 'delete', '--yes', session_id], timeout=30)
    if out is None:
        return [], [f'hermes sessions delete für {session_id} fehlgeschlagen']
    # Cache-Eintrag mit entfernen, sonst wächst hermes_title_cache.json unbegrenzt
    # mit Titeln für längst gelöschte Sessions weiter.
    cache = load_hermes_title_cache()
    if session_id in cache:
        del cache[session_id]
        save_hermes_title_cache(cache)
    return [f'hermes session {session_id}'], []


def _parse_hermes_profiles_table(text):
    """Parst `hermes profile list`. Spalten durch 2+ Leerzeichen getrennt; erste
    Spalte = Profilname (◆ markiert das aktive sticky-default-Profil).
    Liefert [{name, active, model}]."""
    lines = text.splitlines()
    header_idx = next((i for i, l in enumerate(lines)
                       if 'Profile' in l and 'Model' in l), None)
    if header_idx is None:
        return []
    profiles = []
    for line in lines[header_idx + 1:]:
        s = line.strip()
        if not s or set(s) <= set('─-—│| '):  # leer oder Trennlinie (inkl. Leerzeichen)
            continue
        parts = re.split(r'\s{2,}', s)
        first = parts[0]
        active = '◆' in first
        name = first.replace('◆', '').strip()
        if not name:
            continue
        profiles.append({
            'name':   name,
            'active': active,
            'model':  parts[1] if len(parts) > 1 else '',
        })
    return profiles


def send_hermes_profiles(cfg):
    """Liest `hermes profile list` und schickt die Profile an den Server.
    Wird via MQTT action=list-hermes-profiles ausgelöst (App: Profil-Auswahl)."""
    profiles = []
    try:
        out = _hermes_run(cfg, ['profile', 'list'], timeout=30)
        if out:
            profiles = _parse_hermes_profiles_table(out)
    except Exception as e:
        log.error(f'send_hermes_profiles: {e}')
    try:
        r = requests.post(
            cfg['server_url'] + '/put_bridge_hermes_profiles.php',
            json={'token_b': cfg['token_b'], 'profiles': profiles},
            timeout=20,
        )
        data = r.json()
        if 'token_b_new' in data:
            cfg['token_b'] = data['token_b_new']
            save_config(cfg)
        if data.get('status') == 'ok':
            log.info(f'Hermes profiles sent ({len(profiles)})')
        else:
            log.error(f'send_hermes_profiles: server rejected (HTTP {r.status_code}): {data}')
    except Exception as e:
        log.error(f'send_hermes_profiles: post failed: {e}')


def _normalize_hermes_models(data):
    """Normalisiert provider_models_cache.json in eine einheitliche Liste
    [{'id': ..., 'name': ..., 'provider': ...}]. 'provider' ist der
    provider_models_cache.json-Schlüssel (z.B. "openrouter"/"anthropic"),
    damit die App/Web-UI anzeigen kann, über welchen Zugang ein Modell
    erreichbar ist — sonst nicht aus der (teils bereits vendor-qualifizierten)
    'id' allein ablesbar. Tatsächliche Struktur (per Nutzer-Testdatei
    bestätigt): {"<provider>": {"fp": ..., "at": ..., "models": ["<id>", ...]},
    ...} — z.B. {"openrouter": {"models": ["anthropic/claude-opus-5", ...]},
    "anthropic": {"models": ["claude-opus-4-8", ...]}}. Modelle unter dem
    Meta-Provider "openrouter" sind bereits vendor-qualifiziert (enthalten
    "/"); Modelle unter einem direkten Provider (z.B. "anthropic") sind bare
    Namen und werden hier mit dem Provider-Schlüssel präfixiert, damit das
    Ergebnis IMMER im von `hermes chat -m` erwarteten "<provider>/<model>"-
    Format vorliegt (siehe Beispiel "anthropic/claude-sonnet-4" in
    `hermes chat --help`). Dasselbe Modell taucht oft doppelt auf (einmal
    vendor-qualifiziert über "openrouter", einmal bare unter seinem direkten
    Provider) — Ergebnis wird daher über die finale ID dedupliziert, erste
    Fundstelle gewinnt (Reihenfolge der provider_models_cache.json-Keys)."""
    if not isinstance(data, dict):
        return []
    seen = set()
    models = []
    for provider_key, provider_value in data.items():
        if not isinstance(provider_value, dict):
            continue
        raw_models = provider_value.get('models')
        if not isinstance(raw_models, list):
            continue
        for entry in raw_models:
            if not isinstance(entry, str) or not entry:
                continue
            model_id = entry if '/' in entry else f'{provider_key}/{entry}'
            if model_id in seen:
                continue
            seen.add(model_id)
            models.append({'id': model_id, 'name': model_id, 'provider': provider_key})
    return models


def send_hermes_models(cfg):
    """Liest provider_models_cache.json aus dem Home des AKTUELL AKTIVEN
    Hermes-Profils (reiner Dateizugriff, kein Subprocess nötig) und schickt die
    Modelle an den Server. Wird via MQTT action=list-hermes-models ausgelöst
    (App: Modell-Auswahl). Kommt die Liste leer an, im Log nachsehen, welche
    Rohform die Datei tatsächlich hat — _normalize_hermes_models() ggf. anpassen."""
    models = []
    try:
        cache_path = Path(_hermes_home_for(cfg.get('hermes_profile', ''))) / 'provider_models_cache.json'
        if cache_path.is_file():
            with open(cache_path, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            models = _normalize_hermes_models(raw)
        else:
            log.warning(f'send_hermes_models: {cache_path} not found')
    except Exception as e:
        log.error(f'send_hermes_models: {e}')
    try:
        r = requests.post(
            cfg['server_url'] + '/put_bridge_hermes_models.php',
            json={'token_b': cfg['token_b'], 'models': models},
            timeout=20,
        )
        data = r.json()
        if 'token_b_new' in data:
            cfg['token_b'] = data['token_b_new']
            save_config(cfg)
        if data.get('status') == 'ok':
            log.info(f'Hermes models sent ({len(models)})')
        else:
            log.error(f'send_hermes_models: server rejected (HTTP {r.status_code}): {data}')
    except Exception as e:
        log.error(f'send_hermes_models: post failed: {e}')


# --- Dispatch (Backend anhand cli_command wählen) ---

def _history_session_files(cfg):
    binary = _cli_binary(cfg)
    if not binary:
        return [], None
    if 'openclaw' in os.path.basename(binary):
        return _openclaw_session_files(cfg, binary), 'openclaw'
    return _claude_session_files(cfg), 'claude'


def build_history_list(cfg):
    if _is_hermes_cfg(cfg):
        return _hermes_history_list(cfg)
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
    if _is_hermes_cfg(cfg):
        return _hermes_history_detail(cfg, session_id)
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


def build_history_delete_paths(cfg, session_id):
    """Ermittelt die zu löschenden Dateien für eine Session. Claude: nur die
    .jsonl (Hilfsverzeichnisse unter session-env/ und file-history/ werden
    bewusst nicht angefasst — verwaistes Datenmüll ohne Funktion, aber ein
    Löschversuch dort wäre riskanter als der Nutzen). Openclaw: die
    .trajectory.jsonl plus das zugehörige .trajectory-path.json-Sidecar."""
    files, backend = _history_session_files(cfg)
    if backend is None:
        return []
    for path in files:
        if path.stem == session_id or path.name.startswith(session_id):
            if backend == 'claude':
                return [path]
            sidecar = path.with_name(path.name[:-len('.trajectory.jsonl')] + '.trajectory-path.json')
            return [path, sidecar] if sidecar.exists() else [path]
    return []


def delete_history_session(cfg, session_id):
    """Löscht die Datei(en) einer Gesprächs-Session lokal auf dem Agent-Rechner.
    Wird via MQTT action=delete-history-session ausgelöst (Swipe-to-Delete im
    Verlauf-Tab). Die App entfernt den Eintrag bereits optimistisch aus der
    Liste, ohne auf eine Bestätigung zu warten — das Ergebnis geht hier nur
    zu Logging-Zwecken an den Server."""
    errors = []
    deleted = []
    if not session_id:
        errors.append('keine session_id übergeben')
    elif _is_hermes_cfg(cfg):
        # Hermes: keine Dateien — Löschen über `hermes sessions delete --yes <id>`.
        deleted, errors = _hermes_delete_session(cfg, session_id)
    else:
        try:
            paths = build_history_delete_paths(cfg, session_id)
        except Exception as e:
            paths = []
            errors.append(f'Ermittlung der Pfade fehlgeschlagen: {e}')
        if not paths and not errors:
            errors.append(f'keine Datei für session_id={session_id} gefunden')
        for path in paths:
            try:
                path.unlink()
                deleted.append(str(path))
            except FileNotFoundError:
                pass
            except Exception as e:
                errors.append(f'{path}: {e}')

    try:
        r = requests.post(
            cfg['server_url'] + '/put_bridge_history.php',
            json={
                'token_b': cfg['token_b'],
                'kind': 'delete',
                'session_id': session_id,
                'success': not errors,
                'detail': '; '.join(errors) if errors else f'{len(deleted)} Datei(en) gelöscht',
            },
            timeout=20,
        )
        data = r.json()
        if 'token_b_new' in data:
            cfg['token_b'] = data['token_b_new']
            save_config(cfg)
        if data.get('status') == 'ok':
            log.info(f'delete_history_session: session={session_id} deleted={deleted} errors={errors}')
        else:
            log.error(f'delete_history_session: server rejected report (HTTP {r.status_code}): {data}')
    except Exception as e:
        log.error(f'delete_history_session: report post failed: {e}')


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
def _on_connect_tasks(cfg):
    # NACHEINANDER in einem einzigen Thread, nicht als zwei parallele Threads:
    # Token-B ist single-use (jeder Request rotiert ihn serverseitig). Liefen
    # send_pong() und apply_config_update() gleichzeitig, lasen beide denselben
    # cfg['token_b'], und wer beim Server als Zweiter ankam bekam token_b_invalid
    # (der Erste hatte ihn bereits rotiert) — genau das verursachte die
    # "Unexpected ping response: {'error': 'token_b_invalid'}"-Fehler nach jedem
    # Connect. Sequenziell verwenden beide immer den aktuell gültigen Token.
    send_pong(cfg)
    # Proaktiv statt nur MQTT-getriggert: eine frisch angelegte Bridge/Profil hat
    # evtl. noch nie ein 'update-config' erhalten (z.B. weil der Nutzer nach dem
    # Pairing nie manuell auf "Speichern" getippt hat, obwohl das Profil
    # serverseitig schon Standard-Werte hat). apply_config_update() ist ein No-Op,
    # wenn nichts aussteht (bridge_config_pending=0) — bei jedem (Re-)Connect
    # aufzurufen ist daher unschädlich und schließt genau diese Lücke.
    apply_config_update(cfg)


def on_connect(client, userdata, flags, rc):
    cfg = userdata
    if rc == 0:
        topic = f"ac/{cfg['token_a']}"
        client.subscribe(topic, qos=1)
        log.info(f'Connected to MQTT broker, subscribed to: {topic}')
        threading.Thread(target=_on_connect_tasks, args=(cfg,), daemon=True).start()
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
    elif action == 'delete-history-session':
        delete_history_session(cfg, payload.get('session_id', ''))
    elif action == 'send-session-file':
        send_session_file(cfg, payload.get('file_path', ''))
    elif action == 'list-hermes-profiles':
        send_hermes_profiles(cfg)
    elif action == 'list-hermes-models':
        send_hermes_models(cfg)
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
    apply_local_telegram_config(cfg)
    apply_nvm_path_fallback()
    apply_user_local_bin_path()

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