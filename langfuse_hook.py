#!/usr/bin/env python3
"""Claude-Code-Hook, der Tool-Call-Spans an ein self-hosted Langfuse schickt.

Registriert von ac_bridge.py (_ensure_langfuse_hook_registered) für PreToolUse/
PostToolUse/PostToolUseFailure, sobald LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY
gesetzt sind (siehe ac_bridge.env). Ohne diese Variablen ist dieses Skript ein
sofortiges No-Op — unabhängig davon, wer/wie `claude` gestartet hat (ac-bridge
oder andere Automatisierung), solange die Hooks dort ebenfalls registriert sind.

PreToolUse und PostToolUse sind zwei getrennte, zustandslose Prozessaufrufe ohne
gemeinsamen Speicher. Statt eines "create dann update"-Patterns gegen Langfuse
(unsicher, ob das self-hosted v4-Deployment das für Trace-/Observation-Events
akzeptiert) wird der Start (PreToolUse) nur kurz lokal zwischengespeichert und
bei PostToolUse zu EINEM vollständigen Span (Start+Ende+Input+Output) zusammen-
gebaut und in einem einzigen Request gesendet — reine Python-Standardbibliothek,
keine Langfuse-SDK-Abhängigkeit nötig.

Jeder Fehler wird verschluckt (nur geloggt) — ein Langfuse-Ausfall darf den
Agenten nie blockieren oder verlangsamen.
"""
import base64
import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

LANGFUSE_BASE_URL = os.environ.get('LANGFUSE_BASE_URL', '').rstrip('/')
LANGFUSE_PUBLIC_KEY = os.environ.get('LANGFUSE_PUBLIC_KEY', '')
LANGFUSE_SECRET_KEY = os.environ.get('LANGFUSE_SECRET_KEY', '')

STATE_DIR = Path('/tmp/claude-langfuse-hook')


def _log(msg):
    # Kein eigenes Logfile — stderr landet im Claude-Code-Hook-Log, reicht für
    # dieses kleine, seltene Fehlerdiagnose-Bedürfnis.
    print(f'[langfuse_hook] {msg}', file=sys.stderr)


def _now_iso():
    return time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime())


def _state_path(session_id, tool_use_id):
    return STATE_DIR / session_id / f'{tool_use_id}.json'


def _send_span(trace_id, span_id, name, start_time, end_time, span_input, span_output):
    body = {
        'batch': [{
            'id': f'{span_id}-event',
            'type': 'span-create',
            'timestamp': _now_iso(),
            'body': {
                'id': span_id,
                'traceId': trace_id,
                'name': name,
                'startTime': start_time,
                'endTime': end_time,
                'input': span_input,
                'output': span_output,
            },
        }],
    }
    auth = base64.b64encode(f'{LANGFUSE_PUBLIC_KEY}:{LANGFUSE_SECRET_KEY}'.encode()).decode()
    req = urllib.request.Request(
        f'{LANGFUSE_BASE_URL}/api/public/ingestion',
        data=json.dumps(body).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'Authorization': f'Basic {auth}'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        _log(f'send failed for span {span_id}: {e}')


def main():
    if not (LANGFUSE_BASE_URL and LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY):
        return  # No-Op: keine der drei Variablen gesetzt (siehe Modul-Docstring).

    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as e:
        _log(f'invalid stdin JSON: {e}')
        return

    event = payload.get('hook_event_name', '')
    session_id = payload.get('session_id', '')
    tool_use_id = payload.get('tool_use_id', '')
    if not session_id or not tool_use_id:
        return

    if event == 'PreToolUse':
        state = {
            'start_time': _now_iso(),
            'tool_name': payload.get('tool_name', ''),
            'tool_input': payload.get('tool_input'),
        }
        path = _state_path(session_id, tool_use_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(state, f)
        except OSError as e:
            _log(f'could not write state for {tool_use_id}: {e}')
        return

    if event in ('PostToolUse', 'PostToolUseFailure'):
        path = _state_path(session_id, tool_use_id)
        state = {}
        if path.exists():
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    state = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                _log(f'could not read state for {tool_use_id}: {e}')
            try:
                path.unlink()
            except OSError:
                pass

        start_time = state.get('start_time', _now_iso())
        tool_name = state.get('tool_name') or payload.get('tool_name', 'unknown_tool')
        tool_input = state.get('tool_input')
        output = payload.get('tool_response') if event == 'PostToolUse' else {'error': payload.get('error')}

        _send_span(
            trace_id=session_id,
            span_id=tool_use_id,
            name=tool_name,
            start_time=start_time,
            end_time=_now_iso(),
            span_input=tool_input,
            span_output=output,
        )


if __name__ == '__main__':
    main()
