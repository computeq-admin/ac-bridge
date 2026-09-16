#!/usr/bin/env python3
"""Trägt die Langfuse-Hook-Einträge additiv in <cli_working_dir>/.claude/settings.local.json
ein, falls sie dort noch fehlen — bestehende, fremde Hook-Einträge bleiben unangetastet.

Extrahiert aus ac_bridge.py, damit dieselbe Logik sowohl dort (in-process,
gated auf das eigene env-Dict aus cli_env/ac_bridge.env) als auch von
unabhängigen Automatisierungsskripten genutzt werden kann, die `claude` direkt
aufrufen statt über eine ac-bridge (z.B. check_mail_agent.sh, per Cron
getriggert) — reine Python-Standardbibliothek, kein venv-Zwang (wie
langfuse_hook.py selbst), daher mit jedem `python3` aufrufbar.

CLI-Aufruf: python3 ensure_langfuse_hook.py <cli_working_dir>
Erwartet LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY bereits in der Umgebung des
aufrufenden Prozesses (z.B. durch vorheriges `source ac_bridge.env`) — fehlen
sie, macht der CLI-Einstiegspunkt nichts (siehe __main__). Die importierbare
Funktion ensure_registered() selbst prüft das NICHT noch einmal — ac_bridge.py
entscheidet das anhand seines eigenen env-Dicts (nicht os.environ, siehe dort)
VOR dem Aufruf.
"""
import json
import os
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent


def ensure_registered(cli_working_dir):
    if not cli_working_dir:
        return
    project_dir = Path(os.path.expanduser(cli_working_dir))
    if not project_dir.is_dir():
        return
    settings_path = project_dir / '.claude' / 'settings.local.json'

    try:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        if settings_path.exists():
            with open(settings_path, 'r', encoding='utf-8') as f:
                settings = json.load(f)
        else:
            settings = {}
    except (OSError, json.JSONDecodeError) as e:
        print(f'[ensure_langfuse_hook] {settings_path} nicht lesbar: {e}', file=sys.stderr)
        return

    # Bewusst "python3" statt eines absoluten venv-Interpreter-Pfads: wird
    # sowohl von ac_bridge.py (eigenes venv) als auch von unabhängigen
    # Bash-Skripten (ohne dieses venv) aufgerufen — langfuse_hook.py braucht
    # keine Abhängigkeiten, jedes python3 auf PATH reicht. Ein fest kodierter
    # Interpreter-Pfad würde je nach Aufrufer unterschiedlich aussehen und
    # denselben Hook doppelt registrieren (-> doppelt gesendete Spans).
    hook_python = 'python3'
    hook_script = str(REPO_DIR / 'langfuse_hook.py')

    changed = False
    hooks = settings.setdefault('hooks', {})
    for event_name in (
        'PreToolUse', 'PostToolUse', 'PostToolUseFailure',
        'UserPromptSubmit', 'Stop', 'StopFailure', 'SubagentStart', 'SubagentStop',
    ):
        entries = hooks.setdefault(event_name, [])
        already_registered = any(
            h.get('type') == 'command' and h.get('command') == hook_python and h.get('args') == [hook_script]
            for entry in entries
            for h in entry.get('hooks', [])
        )
        if not already_registered:
            entries.append({
                'hooks': [{'type': 'command', 'command': hook_python, 'args': [hook_script], 'timeout': 10}],
            })
            changed = True

    if changed:
        try:
            with open(settings_path, 'w', encoding='utf-8') as f:
                json.dump(settings, f, indent=2)
            print(f'[ensure_langfuse_hook] Langfuse-Hooks in {settings_path} registriert.', file=sys.stderr)
        except OSError as e:
            print(f'[ensure_langfuse_hook] {settings_path} nicht schreibbar: {e}', file=sys.stderr)


if __name__ == '__main__':
    if (
        len(sys.argv) > 1
        and os.environ.get('LANGFUSE_PUBLIC_KEY')
        and os.environ.get('LANGFUSE_SECRET_KEY')
    ):
        ensure_registered(sys.argv[1])
