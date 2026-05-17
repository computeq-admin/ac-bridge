#!/usr/bin/env python3
import sys
from pathlib import Path
_venv_site = next((Path(__file__).parent / 'venv').glob('lib/python*/site-packages'), None)
if _venv_site:
    sys.path.insert(0, str(_venv_site))
"""
AC Bridge Setup
Führt die Ersteinrichtung der Bridge durch:
  1. Token-A generieren
  2. Token-A anzeigen → User trägt es im Webportal ein
  3. OTT vom User entgegennehmen
  4. OTT beim Server einlösen → Token-B + MQTT-Daten erhalten
  5. Agent-Endpunkt konfigurieren
  6. config.json speichern

Usage:
  python3 setup.py
  oder: ./start.sh --setup
"""

import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

import requests

CONFIG_FILE = Path(__file__).parent / 'config.json'
DEFAULT_SERVER = 'https://agent-connect.computeq.de'

DEFAULT_TELEGRAM_SYSTEM_PROMPT = (
    "Du bist ein hilfreicher Assistent. Deine Antworten werden per Telegram-Nachricht übermittelt.\n"
    "Halte dich an die folgenden Regeln:\n\n"
    "## Formatierung\n"
    "Beschränke dich in den Antworten auf die in Telegram möglichen Markdown-Auszeichnungen."
)


def generate_token_a():
    return secrets.token_hex(32)  # 64 Zeichen hex


def show_qr(text):
    """Zeigt text als QR-Code im Terminal (via qrcode aus der venv)."""
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(text)
        qr.make(fit=True)
        print()
        qr.print_ascii(invert=True)
        print()
    except Exception as e:
        print(f"  (QR-Fehler: {e})")


def load_or_create_config():
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {}


def save_config(cfg):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(cfg, f, indent=2)
    os.chmod(CONFIG_FILE, 0o600)


def sanitize_service_name(email):
    """Erzeugt einen systemd-kompatiblen Service-Namen aus einer E-Mail-Adresse."""
    return 'ac_bridge-' + email.replace('@', '-')


def remove_existing_services(service_name):
    """Stoppt und deaktiviert den angegebenen ac_bridge systemd User-Service, falls er existiert."""
    systemd_dir = Path.home() / '.config' / 'systemd' / 'user'
    service_file = systemd_dir / f'{service_name}.service'
    if not service_file.exists():
        return

    print(f"  Entferne bestehenden Service '{service_name}'...")
    try:
        subprocess.run(['systemctl', '--user', 'stop',    service_name], capture_output=True)
        subprocess.run(['systemctl', '--user', 'disable', service_name], capture_output=True)
        print(f"  ✓ '{service_name}' gestoppt und deaktiviert")
    except FileNotFoundError:
        pass  # systemctl nicht verfügbar, ignorieren

    try:
        subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True, capture_output=True)
        print("  ✓ systemd daemon-reload")
    except Exception:
        pass


def install_service(install_dir, service_name):
    """Installiert ac_bridge als systemd User-Service mit gegebenem Service-Namen."""
    service_template = Path(__file__).parent / 'ac_bridge.service'
    if not service_template.exists():
        print("  ⚠ ac_bridge.service nicht gefunden, übersprungen.")
        return

    content = service_template.read_text()
    content = content.replace('__INSTALL_DIR__', str(install_dir))

    systemd_dir = Path.home() / '.config' / 'systemd' / 'user'
    systemd_dir.mkdir(parents=True, exist_ok=True)

    dest = systemd_dir / f'{service_name}.service'
    dest.write_text(content)
    print(f"  ✓ Service-Datei geschrieben: {dest}")

    try:
        subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
        subprocess.run(['systemctl', '--user', 'enable', '--now', service_name], check=True)
        print(f"  ✓ Service '{service_name}' aktiviert und gestartet")
    except subprocess.CalledProcessError as e:
        print(f"  ⚠ systemctl Fehler: {e}")
        print(f"  Manuell: systemctl --user enable --now {service_name}")
    except FileNotFoundError:
        print("  ⚠ systemctl nicht gefunden. Service manuell installieren:")
        print(f"  cp {dest} ~/.config/systemd/user/")
        print(f"  systemctl --user enable --now {service_name}")


def configure_cli(cfg):
    """Konfiguriert den CLI-Agenten interaktiv"""
    print()
    print("=" * 60)
    print("  KI-Agent CLI konfigurieren")
    print("=" * 60)
    print()

    default = cfg.get('service_name', 'ac_bridge')
    val = input(f"  Systemd Service-Name [{default}]: ").strip()
    cfg['service_name'] = val or default

    default = cfg.get('cli_command', '')
    val = input(f"  CLI Befehl (voller Pfad) [{default or 'z.B. /usr/local/bin/claude'}]: ").strip()
    cfg['cli_command'] = val or default

    default = cfg.get('cli_working_dir', '')
    val = input(f"  Arbeitsverzeichnis [{default or 'leer = Bridge-Verzeichnis'}]: ").strip()
    cfg['cli_working_dir'] = val or default or ''

    default = cfg.get('cli_prompt_param', '-p')
    val = input(f"  Prompt-Parameter [{default}]: ").strip()
    cfg['cli_prompt_param'] = val or default

    default = cfg.get('cli_system_prompt_param', '--system-prompt')
    val = input(f"  System-Prompt-Parameter [{default}]: ").strip()
    cfg['cli_system_prompt_param'] = val or default

    default = cfg.get('cli_session_id_param', '')
    print(f"  Session-Fortsetzungs-Parameter [{default or 'leer = kein Session-Tracking'}]")
    print("  (z.B. --resume — wird mit der Session-ID aus dem letzten Call übergeben)")
    val = input("  > ").strip()
    cfg['cli_session_id_param'] = val if val else default

    default = cfg.get('cli_session_id_output_field', '')
    print(f"  JSON-Feld für Session-ID im Output [{default or 'leer = kein JSON-Parsing'}]")
    print("  (z.B. session_id bei Claude CLI mit --output-format json)")
    val = input("  > ").strip()
    cfg['cli_session_id_output_field'] = val if val else default

    if cfg.get('cli_session_id_output_field'):
        default = cfg.get('cli_answer_output_field', 'result')
        val = input(f"  JSON-Feld für Antworttext im Output [{default}]: ").strip()
        cfg['cli_answer_output_field'] = val or default

    default = cfg.get('cli_timeout', 600)
    val = input(f"  Timeout in Sekunden [{default}]: ").strip()
    cfg['cli_timeout'] = int(val) if val.isdigit() else default

    existing_extra = cfg.get('cli_extra_params', '')
    print(f"  Weitere Parameter [{existing_extra or 'keine'}]")
    print("  (Leerzeichen-getrennt, z.B. --no-color --output-format json)")
    val = input("  > ").strip()
    if val:
        cfg['cli_extra_params'] = val
    elif not existing_extra:
        cfg['cli_extra_params'] = ''

    print()
    print("  Umgebungsvariablen (Format: KEY=VALUE, leere Zeile zum Abschluss)")
    existing_env = cfg.get('cli_env', {})
    if existing_env:
        print(f"  Bestehend: {json.dumps(existing_env, ensure_ascii=False)}")
        keep = input("  Bestehende behalten? (J/n): ").strip().lower()
        if keep == 'n':
            existing_env = {}
    env_vars = dict(existing_env)
    while True:
        val = input("  KEY=VALUE (oder ENTER zum Abschluss): ").strip()
        if not val:
            break
        if '=' in val:
            k, v = val.split('=', 1)
            env_vars[k.strip()] = v.strip()
        else:
            print("  ⚠ Format: KEY=VALUE — übersprungen")
    cfg['cli_env'] = env_vars

    return cfg


def cmd_get_token():
    """--get-token: Token-A anzeigen oder neu generieren (nicht-interaktiv, KI-geeignet)."""
    cfg = load_or_create_config()

    if cfg.get('token_b'):
        print('INFO: Bridge ist bereits vollständig eingerichtet.')
        print(f"Token-A: {cfg['token_a']}")
        show_qr(cfg['token_a'])
        print('Um neu einzurichten: python3 setup.py --setup')
        sys.exit(0)

    if cfg.get('token_a'):
        print('INFO: Setup läuft bereits (Token-A vorhanden, OTT noch nicht eingelöst).')
        print(f"Token-A: {cfg['token_a']}")
        show_qr(cfg['token_a'])
        sys.exit(0)

    token_a = generate_token_a()
    cfg['token_a'] = token_a
    cfg.pop('token_b', None)
    save_config(cfg)
    print(f"Token-A: {token_a}")
    show_qr(token_a)
    sys.exit(0)


def cmd_redeem_ott(ott):
    """--redeem-ott <OTT>: OTT einlösen und Setup abschließen (nicht-interaktiv, KI-geeignet)."""
    cfg = load_or_create_config()

    if not cfg.get('token_a'):
        print('Fehler: Kein Token-A gefunden.')
        print('Bitte zuerst ausführen: python3 setup.py --get-token')
        sys.exit(1)

    print('Löse OTT ein ...')
    try:
        r = requests.post(
            DEFAULT_SERVER + '/redeem_ott.php',
            json={'token_a': cfg['token_a'], 'ott': ott},
            timeout=15,
        )
        data = r.json()
    except Exception as e:
        print(f'Fehler beim Server-Aufruf: {e}')
        sys.exit(1)

    if r.status_code != 200 or 'token_b' not in data:
        err = data.get('error', 'unknown')
        print(f'Fehler: {err}')
        if err == 'ott_expired':
            print('Der OTT ist abgelaufen (10 Min). Bitte neu anfordern.')
        elif err == 'ott_already_used':
            print('Dieser OTT wurde bereits verwendet.')
        elif err == 'token_a_not_found':
            print('Token-A nicht gefunden. Bitte Token-A im Portal zuerst eintragen.')
        sys.exit(1)

    cfg['token_b']       = data['token_b']
    cfg['mqtt_host']     = data['mqtt_host']
    cfg['mqtt_port']     = data['mqtt_port']
    cfg['mqtt_user']     = data['mqtt_user']
    cfg['mqtt_password'] = data['mqtt_password']
    cfg['mqtt_tls']      = data.get('mqtt_tls', False)
    cfg['server_url']    = data.get('server_url', DEFAULT_SERVER)

    user_email          = data.get('user_email', '')
    service_name        = sanitize_service_name(user_email) if user_email else 'ac_bridge'
    cfg['service_name'] = service_name

    cfg.setdefault('cli_command',                '')
    cfg.setdefault('cli_working_dir',            '')
    cfg.setdefault('cli_prompt_param',           '-p')
    cfg.setdefault('cli_system_prompt_param',    '--system-prompt')
    cfg.setdefault('cli_session_id_param',       '')
    cfg.setdefault('cli_session_id_output_field','')
    cfg.setdefault('cli_answer_output_field',    'result')
    cfg.setdefault('cli_timeout',                600)
    cfg.setdefault('cli_extra_params',           '')
    cfg.setdefault('cli_env',                    {})
    cfg['lang'] = 'DE'

    save_config(cfg)
    print(f'✓ Verbindung hergestellt')
    if user_email:
        print(f'✓ User: {user_email}')
    print(f'✓ Service-Name: {service_name}')
    print(f'✓ config.json gespeichert (Berechtigungen: 600)')

    if '--no-service' not in sys.argv:
        remove_existing_services(service_name)
        install_service(Path(__file__).parent, service_name)
        try:
            subprocess.run(
                ['loginctl', 'enable-linger', os.environ.get('USER', '')],
                check=True, capture_output=True,
            )
            print('✓ loginctl linger aktiviert (Service startet ohne Login)')
        except Exception:
            print('  Hinweis: loginctl enable-linger manuell ausführen für Auto-Start')

    print()
    print('Setup abgeschlossen! Die Bridge läuft jetzt als systemd user service.')
    print(f'Logs:    journalctl --user -u {service_name} -f')
    print(f'Status:  systemctl --user status {service_name}')
    sys.exit(0)


def cmd_telegram_only():
    """--telegram-only: Richtet die Bridge ohne MQTT nur für Telegram ein."""
    cfg = load_or_create_config()

    print()
    print("=" * 60)
    print("  AC Bridge — Telegram-Only Setup")
    print("=" * 60)
    print()
    print("  Verbindet Telegram direkt mit dem lokalen KI-Agenten.")
    print("  Keine MQTT/Server-Verbindung erforderlich.")
    print()

    default = cfg.get('user', os.environ.get('USER', ''))
    val = input(f"  Benutzername [{default}]: ").strip()
    cfg['user'] = val or default

    service_name = 'ac_bridge_tg-' + (cfg['user'] or 'default').replace(' ', '_').lower()
    cfg['service_name'] = service_name

    print()
    print("  CLI-Agent (ENTER = Standardwert übernehmen):")
    print()

    default = cfg.get('cli_command', '~/.npm-global/bin/claude')
    val = input(f"  CLI Befehl [{default}]: ").strip()
    cfg['cli_command'] = val or default

    default = cfg.get('cli_working_dir', '~')
    val = input(f"  Arbeitsverzeichnis [{default}]: ").strip()
    cfg['cli_working_dir'] = val or default

    cfg.setdefault('cli_prompt_param',             '-p')
    cfg.setdefault('cli_system_prompt_param',      '--system-prompt')
    cfg.setdefault('cli_session_id_param',         '--resume')
    cfg.setdefault('cli_session_id_output_field',  'session_id')
    cfg.setdefault('cli_answer_output_field',      'result')
    cfg.setdefault('cli_timeout',                  600)
    cfg.setdefault('cli_extra_params',             '--output-format json --dangerously-skip-permissions')
    cfg.setdefault('cli_file_param',               '')
    cfg.setdefault('cli_env',                      {})
    cfg.setdefault('lang',                         'DE')

    print()
    print("  Telegram Bot-Konfiguration:")
    print()

    default = cfg.get('telegram_bot_token', '')
    while True:
        hint = f'bestehend: ...{default[-6:]}' if default else 'erforderlich'
        val = input(f"  Bot-Token von @BotFather [{hint}]: ").strip()
        token = val or default
        if token:
            break
        print("  ⚠ Bot-Token ist erforderlich.")
    cfg['telegram_bot_token'] = token

    default = cfg.get('telegram_chat_id', '')
    while True:
        hint = default if default else 'erforderlich — Tipp: @userinfobot fragen'
        val = input(f"  Chat-ID (deine Telegram User-ID) [{hint}]: ").strip()
        chat_id = val or default
        if chat_id:
            break
        print("  ⚠ Chat-ID ist erforderlich.")
    cfg['telegram_chat_id'] = chat_id

    print()
    current_sp = cfg.get('telegram_system_prompt', DEFAULT_TELEGRAM_SYSTEM_PROMPT)
    preview = current_sp[:80].replace('\n', ' ')
    print(f"  System-Prompt (ENTER = aktuellen Wert behalten):")
    print(f"  Aktuell: {preview}...")
    val = input("  Neuer System-Prompt (oder ENTER): ").strip()
    cfg['telegram_system_prompt'] = val if val else current_sp

    cfg['telegram_only'] = True

    save_config(cfg)
    print()
    print("  ✓ config.json gespeichert (Berechtigungen: 600).")

    print()
    print("=" * 60)
    print("  Systemd User-Service einrichten")
    print("=" * 60)
    print()
    print(f"  Service-Name: {service_name}")
    print()
    ans = input("  Service jetzt einrichten und starten? (J/n): ").strip().lower()
    if ans != 'n':
        remove_existing_services(service_name)
        install_service(Path(__file__).parent, service_name)
        try:
            subprocess.run(
                ['loginctl', 'enable-linger', os.environ.get('USER', '')],
                check=True, capture_output=True,
            )
            print('  ✓ loginctl linger aktiviert (Service startet ohne Login)')
        except Exception:
            print('  Hinweis: loginctl enable-linger manuell ausführen für Auto-Start')

    print()
    print("=" * 60)
    print("  Setup abgeschlossen!")
    print("=" * 60)
    print()
    print(f"  Logs:    journalctl --user -u {service_name} -f")
    print(f"  Status:  systemctl --user status {service_name}")
    print(f"  Stopp:   systemctl --user stop {service_name}")
    print()
    sys.exit(0)


def main():
    # ── Telegram-Only Setup ───────────────────────────────────────────────────
    if '--telegram-only' in sys.argv:
        cmd_telegram_only()

    # ── Nicht-interaktive Befehle (KI-geeignet) ──────────────────────────────
    if '--get-token' in sys.argv:
        cmd_get_token()

    if '--redeem-ott' in sys.argv:
        idx = sys.argv.index('--redeem-ott')
        if idx + 1 >= len(sys.argv):
            print('Fehler: Kein OTT angegeben.')
            print('Verwendung: python3 setup.py --redeem-ott <OTT>')
            sys.exit(1)
        cmd_redeem_ott(sys.argv[idx + 1].strip())

    # ── Nur Agent-Konfiguration (ohne Neuverbindung) ──────────────────────────
    if '--config' in sys.argv:
        cfg = load_or_create_config()
        if not cfg.get('token_b'):
            print("Keine bestehende Verbindung gefunden. Bitte zuerst Setup ausführen:")
            print("  ./start.sh --setup")
            sys.exit(1)
        print()
        print("=" * 60)
        print("  AC Bridge — Agent-Konfiguration")
        print("=" * 60)
        cfg = configure_cli(cfg)
        save_config(cfg)
        print()
        print("  ✓ config.json aktualisiert.")
        print()
        svc = cfg.get('service_name', 'ac_bridge')
        print("  Bridge neu starten um Änderungen zu übernehmen:")
        print(f"  systemctl --user restart {svc}")
        print("  oder: ./start.sh")
        print()
        sys.exit(0)

    print()
    print("=" * 60)
    print("  AC Bridge Setup — Agent Connect")
    print("=" * 60)
    print()

    cfg = load_or_create_config()

    # ── Neu konfigurieren? ────────────────────
    if cfg.get('token_a') and cfg.get('token_b'):
        print("Bestehende Konfiguration gefunden.")
        reconfig = input("Neu konfigurieren? (j/N): ").strip().lower()
        if reconfig != 'j':
            print("Setup abgebrochen. Bestehende config.json bleibt erhalten.")
            sys.exit(0)

    # ── Token-A generieren ────────────────────
    token_a = generate_token_a()
    cfg['token_a'] = token_a
    cfg.pop('token_b', None)
    save_config(cfg)

    print()
    print("=" * 60)
    print("  SCHRITT 1: Token-A")
    print("=" * 60)
    print()
    print("  Dein Token-A (Bridge-Identifikation):")
    print()
    print(f"  >>> {token_a} <<<")
    show_qr(token_a)
    print()
    print("  Scanne den QR-Code mit der Agent-Talk iOS App (Konfiguration → Bridge)")
    print("  oder trage den Token manuell ins Webportal ein:")
    print(f"  {DEFAULT_SERVER}/index.php")
    print()
    print("  Du erhältst einen One-Time-Token (OTT) zurück.")
    print()
    input("  Drücke ENTER wenn du den OTT erhalten hast...")

    # ── OTT einlösen ──────────────────────────
    print()
    print("=" * 60)
    print("  SCHRITT 2: OTT einlösen")
    print("=" * 60)
    print()
    ott = input("  OTT aus dem Webportal eingeben: ").strip()

    if not ott:
        print("Kein OTT eingegeben. Abbruch.")
        sys.exit(1)

    print()
    print("  Verbinde mit Server...")

    try:
        r = requests.post(
            DEFAULT_SERVER + '/redeem_ott.php',
            json={'token_a': token_a, 'ott': ott},
            timeout=15,
        )
        data = r.json()
    except Exception as e:
        print(f"\n  Fehler beim Server-Aufruf: {e}")
        sys.exit(1)

    if r.status_code != 200 or 'token_b' not in data:
        err = data.get('error', 'unknown')
        print(f"\n  Fehler: {err}")
        if err == 'ott_expired':
            print("  Der OTT ist abgelaufen (10 Min). Bitte neu anfordern.")
        elif err == 'ott_already_used':
            print("  Dieser OTT wurde bereits verwendet.")
        elif err == 'token_a_not_found':
            print("  Token-A nicht gefunden. Bitte Token-A im Portal eintragen.")
        sys.exit(1)

    # MQTT-Daten + Token-B + User-Email vom Server übernehmen
    cfg['token_b']       = data['token_b']
    cfg['mqtt_host']     = data['mqtt_host']
    cfg['mqtt_port']     = data['mqtt_port']
    cfg['mqtt_user']     = data['mqtt_user']
    cfg['mqtt_password'] = data['mqtt_password']
    cfg['mqtt_tls']      = data.get('mqtt_tls', False)
    cfg['server_url']    = data.get('server_url', DEFAULT_SERVER)

    user_email           = data.get('user_email', '')
    service_name         = sanitize_service_name(user_email) if user_email else 'ac_bridge'
    cfg['service_name']  = service_name

    print(f"  ✓ Token-B erhalten")
    print(f"  ✓ MQTT-Zugangsdaten erhalten")
    if user_email:
        print(f"  ✓ User: {user_email}")
    print(f"  ✓ Service-Name: {service_name}")

    # Minimale CLI-Defaults — werden später per Web-Backend konfiguriert
    cfg.setdefault('cli_command',                '')
    cfg.setdefault('cli_working_dir',            '')
    cfg.setdefault('cli_prompt_param',           '-p')
    cfg.setdefault('cli_system_prompt_param',    '--system-prompt')
    cfg.setdefault('cli_session_id_param',       '')
    cfg.setdefault('cli_session_id_output_field','')
    cfg.setdefault('cli_answer_output_field',    'result')
    cfg.setdefault('cli_timeout',                600)
    cfg.setdefault('cli_extra_params',           '')
    cfg.setdefault('cli_env',                    {})
    cfg['lang'] = 'DE'

    # ── Speichern ─────────────────────────────
    save_config(cfg)
    print("  ✓ config.json gespeichert (Berechtigungen: 600).")
    print()
    print("  Hinweis: CLI-Konfiguration (Agent, Parameter) bitte")
    print("  im Web-Backend unter 'Bridge konfigurieren' vornehmen.")

    # ── Service installieren ───────────────────
    print()
    print("=" * 60)
    print("  SCHRITT 3: Systemd User-Service")
    print("=" * 60)
    print()
    print("  Der Bridge-Service läuft als dein Benutzer (kein sudo nötig).")
    print(f"  Service-Name: {service_name}")
    print()
    ans = input("  Service jetzt einrichten und starten? (J/n): ").strip().lower()
    if ans != 'n':
        remove_existing_services(service_name)
        install_service(Path(__file__).parent, service_name)
        print()
        print("  Tipp: Damit der Service auch ohne Login startet:")
        print("  loginctl enable-linger $USER")
    else:
        install_dir = Path(__file__).parent
        print()
        print("  Manuell einrichten:")
        print(f"  systemctl --user enable --now {service_name}")

    print()
    print("=" * 60)
    print("  Setup abgeschlossen!")
    print("=" * 60)
    print()
    print("  Bridge manuell starten (ohne Service):")
    print("  ./start.sh")
    print()
    print("  Service-Befehle:")
    print("  systemctl --user status ac_bridge")
    print("  systemctl --user restart ac_bridge")
    print("  journalctl --user -u ac_bridge -f")
    print()


if __name__ == '__main__':
    main()