#!/usr/bin/env python3
"""
AAT Bridge Setup
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
import shlex
import subprocess
import sys
from pathlib import Path

import requests

CONFIG_FILE = Path(__file__).parent / 'config.json'
DEFAULT_SERVER = 'https://ai-agent-tasks.computeq.de'


def generate_token_a():
    return secrets.token_hex(32)  # 64 Zeichen hex


def load_or_create_config():
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {}


def save_config(cfg):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(cfg, f, indent=2)
    os.chmod(CONFIG_FILE, 0o600)


def install_service(install_dir):
    """Installiert aat_bridge als systemd User-Service"""
    service_template = Path(__file__).parent / 'aat_bridge.service'
    if not service_template.exists():
        print("  ⚠ aat_bridge.service nicht gefunden, übersprungen.")
        return

    content = service_template.read_text()
    content = content.replace('__INSTALL_DIR__', str(install_dir))

    systemd_dir = Path.home() / '.config' / 'systemd' / 'user'
    systemd_dir.mkdir(parents=True, exist_ok=True)

    dest = systemd_dir / 'aat_bridge.service'
    dest.write_text(content)
    print(f"  ✓ Service-Datei geschrieben: {dest}")

    try:
        subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
        subprocess.run(['systemctl', '--user', 'enable', '--now', 'aat_bridge'], check=True)
        print("  ✓ Service aktiviert und gestartet")
    except subprocess.CalledProcessError as e:
        print(f"  ⚠ systemctl Fehler: {e}")
        print("  Manuell: systemctl --user enable --now aat_bridge")
    except FileNotFoundError:
        print("  ⚠ systemctl nicht gefunden. Service manuell installieren:")
        print(f"  cp {dest} ~/.config/systemd/user/")
        print("  systemctl --user enable --now aat_bridge")


def configure_cli(cfg):
    """Konfiguriert den CLI-Agenten interaktiv"""
    print()
    print("=" * 60)
    print("  KI-Agent CLI konfigurieren")
    print("=" * 60)
    print()

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

    existing_extra = cfg.get('cli_extra_params', [])
    default_str = ' '.join(existing_extra) if existing_extra else 'keine'
    print(f"  Weitere Parameter [{default_str}]")
    print("  (Leerzeichen-getrennt, z.B. --no-color --output-format text)")
    val = input("  > ").strip()
    if val:
        cfg['cli_extra_params'] = shlex.split(val)
    elif not existing_extra:
        cfg['cli_extra_params'] = []

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


def main():
    # ── Nur Agent-Konfiguration (ohne Neuverbindung) ──────────────────────────
    if '--config' in sys.argv:
        cfg = load_or_create_config()
        if not cfg.get('token_b'):
            print("Keine bestehende Verbindung gefunden. Bitte zuerst Setup ausführen:")
            print("  ./start.sh --setup")
            sys.exit(1)
        print()
        print("=" * 60)
        print("  AAT Bridge — Agent-Konfiguration")
        print("=" * 60)
        cfg = configure_cli(cfg)
        save_config(cfg)
        print()
        print("  ✓ config.json aktualisiert.")
        print()
        print("  Bridge neu starten um Änderungen zu übernehmen:")
        print("  systemctl --user restart aat_bridge")
        print("  oder: ./start.sh")
        print()
        sys.exit(0)

    print()
    print("=" * 60)
    print("  AAT Bridge Setup — AI Agent Tasks")
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
    print()
    print("  Trage diesen Token jetzt in dein Webportal ein:")
    print(f"  {DEFAULT_SERVER}/index.php")
    print()
    print("  Das Portal gibt dir einen One-Time-Token (OTT) zurück.")
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

    # MQTT-Daten + Token-B vom Server übernehmen
    cfg['token_b']       = data['token_b']
    cfg['mqtt_host']     = data['mqtt_host']
    cfg['mqtt_port']     = data['mqtt_port']
    cfg['mqtt_user']     = data['mqtt_user']
    cfg['mqtt_password'] = data['mqtt_password']
    cfg['mqtt_tls']      = data.get('mqtt_tls', False)
    cfg['server_url']    = data.get('server_url', DEFAULT_SERVER)

    print("  ✓ Token-B erhalten")
    print("  ✓ MQTT-Zugangsdaten erhalten")

    # ── Agent konfigurieren ───────────────────
    print()
    print("=" * 60)
    print("  SCHRITT 3: KI-Agent konfigurieren")
    print("=" * 60)
    cfg = configure_cli(cfg)
    cfg['lang'] = 'DE'

    # ── Speichern ─────────────────────────────
    save_config(cfg)
    print("  ✓ config.json gespeichert (Berechtigungen: 600).")

    # ── Service installieren ───────────────────
    print()
    print("=" * 60)
    print("  SCHRITT 4: Systemd User-Service")
    print("=" * 60)
    print()
    print("  Der Bridge-Service läuft als dein Benutzer (kein sudo nötig).")
    print()
    ans = input("  Service jetzt einrichten und starten? (J/n): ").strip().lower()
    if ans != 'n':
        install_service(Path(__file__).parent)
        print()
        print("  Tipp: Damit der Service auch ohne Login startet:")
        print("  loginctl enable-linger $USER")
    else:
        install_dir = Path(__file__).parent
        print()
        print("  Manuell einrichten:")
        print(f"  python3 -c \"")
        print(f"    from pathlib import Path; import setup")
        print(f"    setup.install_service(Path('{install_dir}'))\"")
        print("  oder: systemctl --user enable --now aat_bridge")

    print()
    print("=" * 60)
    print("  Setup abgeschlossen!")
    print("=" * 60)
    print()
    print("  Bridge manuell starten (ohne Service):")
    print("  ./start.sh")
    print()
    print("  Service-Befehle:")
    print("  systemctl --user status aat_bridge")
    print("  systemctl --user restart aat_bridge")
    print("  journalctl --user -u aat_bridge -f")
    print()


if __name__ == '__main__':
    main()