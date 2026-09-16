#!/usr/bin/env python3
"""Claude-Code-Hook, der Turn-/Sub-Agenten-/Tool-Call-Spans per OTLP/HTTP-JSON
an ein self-hosted Langfuse schickt.

Registriert von ac_bridge.py (_ensure_langfuse_hook_registered) für PreToolUse/
PostToolUse/PostToolUseFailure/UserPromptSubmit/Stop/StopFailure/SubagentStart/
SubagentStop, sobald LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY gesetzt sind (siehe
ac_bridge.env). Ohne diese Variablen ist dieses Skript ein sofortiges No-Op —
unabhängig davon, wer/wie `claude` gestartet hat (ac-bridge oder andere
Automatisierung), solange die Hooks dort ebenfalls registriert sind.

Genutzter Endpunkt: POST /api/public/otel/v1/traces (OTLP/HTTP, JSON-Encoding)
— NICHT die klassische /api/public/ingestion-Batch-API. Grund: das self-hosted
Langfuse lief im v3->v4-Migrationsmodus "events_only"; selbst nach Umstellung
auf "dual" landete ein testweise per Ingestion-API gesendetes Event zwar mit
HTTP 201, aber nie sichtbar in der UI (Dual-Write-Propagation-Job fand keine
Partition zu verarbeiten — interne Migrationslogik, nicht weiter aufgelöst).
Der OTLP-Endpunkt ist der Pfad, der beim bereits funktionierenden LangGraph-
Beispiel (langfuse.langchain.CallbackHandler) nachweislich funktioniert.

Jedes Start/Ende-Paar (UserPromptSubmit/Stop|StopFailure für den Turn,
SubagentStart/SubagentStop für Sub-Agenten, PreToolUse/PostToolUse|
PostToolUseFailure für Tool-Calls) sind zwei getrennte, zustandslose
Prozessaufrufe ohne gemeinsamen Speicher. Der Start-Event wird daher kurz
lokal zwischengespeichert und beim Ende-Event zu EINEM vollständigen OTLP-Span
(Start+Ende+Input+Output) zusammengebaut und in einem einzigen Request
gesendet — reine Python-Standardbibliothek, keine Langfuse-SDK-/OTel-SDK-
Abhängigkeit nötig.

Hierarchie: Turn-Span ist Trace-Root (kein parentSpanId, Korrelations-ID
`prompt_id`). Tool-Call- und Sub-Agenten-Spans hängen als Kinder darunter,
Tool-Calls INNERHALB eines Sub-Agenten hängen unter dessen Span (Korrelations-
ID `agent_id`, laut Claude-Code-Hook-Schema ein gemeinsames Feld auf allen
Events, auch auf verschachtelten Tool-Calls).

Input/Output werden über die von Langfuse dafür vorgesehenen Span-Attribute
`langfuse.observation.input`/`langfuse.observation.output` (JSON-String)
gesetzt (siehe https://langfuse.com/integrations/native/opentelemetry).

Jeder Fehler wird verschluckt (nur geloggt) — ein Langfuse-Ausfall darf den
Agenten nie blockieren oder verlangsamen.
"""
import hashlib
import json
import os
import sys
import time
import urllib.request
import urllib.error
from base64 import b64encode
from pathlib import Path

LANGFUSE_BASE_URL = os.environ.get('LANGFUSE_BASE_URL', '').rstrip('/')
LANGFUSE_PUBLIC_KEY = os.environ.get('LANGFUSE_PUBLIC_KEY', '')
LANGFUSE_SECRET_KEY = os.environ.get('LANGFUSE_SECRET_KEY', '')

STATE_DIR = Path('/tmp/claude-langfuse-hook')


def _log(msg):
    # Kein eigenes Logfile — stderr landet im Claude-Code-Hook-Log, reicht für
    # dieses kleine, seltene Fehlerdiagnose-Bedürfnis.
    print(f'[langfuse_hook] {msg}', file=sys.stderr)


def _state_path(session_id, kind, key):
    # kind ('turn'/'subagent'/'tool') als Präfix, damit die drei ID-Namensräume
    # (prompt_id/agent_id/tool_use_id) sich nie versehentlich überschneiden.
    return STATE_DIR / session_id / f'{kind}-{key}.json'


def _write_state(session_id, kind, key, state):
    if not key:
        return
    path = _state_path(session_id, kind, key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(state, f)
    except OSError as e:
        _log(f'could not write state for {kind}-{key}: {e}')


def _read_and_clear_state(session_id, kind, key):
    if not key:
        return {}
    path = _state_path(session_id, kind, key)
    state = {}
    if path.exists():
        try:
            with open(path, 'r', encoding='utf-8') as f:
                state = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            _log(f'could not read state for {kind}-{key}: {e}')
        try:
            path.unlink()
        except OSError:
            pass
    return state


def _otel_id(value, hex_len):
    """Deterministisch aus einer beliebigen Claude-ID (session_id/tool_use_id/
    prompt_id/agent_id, kein garantiertes Hex-Format) eine gültige OTel-Trace-/
    Span-ID ableiten (32 bzw. 16 Hex-Zeichen)."""
    return hashlib.sha256(value.encode('utf-8')).hexdigest()[:hex_len]


def _json_attr(key, value):
    return {'key': key, 'value': {'stringValue': json.dumps(value, ensure_ascii=False)}}


def _send_span(session_id, span_id_source, name, start_ns, end_ns, span_input, span_output,
                is_error, parent_span_id_source=None):
    attributes = [
        _json_attr('langfuse.observation.input', span_input),
        _json_attr('langfuse.observation.output', span_output),
        {'key': 'langfuse.trace.name', 'value': {'stringValue': f'claude-session-{session_id[:8]}'}},
    ]
    span = {
        'traceId': _otel_id(session_id, 32),
        'spanId': _otel_id(span_id_source, 16),
        'name': name,
        'kind': 1,  # SPAN_KIND_INTERNAL
        # Als String, nicht als JSON-Zahl: uint64-Nanosekunden-Zeitstempel
        # überschreiten Number.MAX_SAFE_INTEGER, ein JS-JSON.parse (Langfuse
        # ist Node/TypeScript) würde sie sonst verlustbehaftet runden — Strings
        # sind die protobuf-JSON-Standardkodierung für int64/uint64-Felder.
        'startTimeUnixNano': str(start_ns),
        'endTimeUnixNano': str(end_ns),
        'attributes': attributes,
        'status': {'code': 2 if is_error else 1},  # STATUS_CODE_ERROR : STATUS_CODE_OK
    }
    if parent_span_id_source:
        span['parentSpanId'] = _otel_id(parent_span_id_source, 16)

    body = {
        'resourceSpans': [{
            'resource': {'attributes': [{'key': 'service.name', 'value': {'stringValue': 'ac-bridge'}}]},
            'scopeSpans': [{
                'scope': {'name': 'ac_bridge.langfuse_hook'},
                'spans': [span],
            }],
        }],
    }
    auth = b64encode(f'{LANGFUSE_PUBLIC_KEY}:{LANGFUSE_SECRET_KEY}'.encode()).decode()
    req = urllib.request.Request(
        f'{LANGFUSE_BASE_URL}/api/public/otel/v1/traces',
        data=json.dumps(body).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'Authorization': f'Basic {auth}'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        _log(f'send failed for span {span_id_source}: {e}')


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
    if not session_id:
        return

    # ── Turn (User-Prompt -> Antwort) — Trace-Root ─────────────────────────
    if event == 'UserPromptSubmit':
        _write_state(session_id, 'turn', payload.get('prompt_id', ''), {
            'start_ns': time.time_ns(),
            'user_prompt': payload.get('user_prompt'),
        })
        return

    if event in ('Stop', 'StopFailure'):
        prompt_id = payload.get('prompt_id', '')
        if not prompt_id:
            return
        state = _read_and_clear_state(session_id, 'turn', prompt_id)
        end_ns = time.time_ns()
        _send_span(
            session_id=session_id,
            span_id_source=prompt_id,
            name='turn',
            start_ns=state.get('start_ns', end_ns),
            end_ns=end_ns,
            span_input=state.get('user_prompt'),
            span_output=payload.get('last_assistant_message'),
            is_error=(event == 'StopFailure'),
        )
        return

    # ── Sub-Agenten (Explore/Plan/etc.) — Kind des Turn-Spans ──────────────
    if event == 'SubagentStart':
        agent_id = payload.get('agent_id', '')
        _write_state(session_id, 'subagent', agent_id, {
            'start_ns': time.time_ns(),
            'agent_type': payload.get('agent_type', ''),
            'prompt_id': payload.get('prompt_id', ''),
        })
        return

    if event == 'SubagentStop':
        agent_id = payload.get('agent_id', '')
        if not agent_id:
            return
        state = _read_and_clear_state(session_id, 'subagent', agent_id)
        end_ns = time.time_ns()
        agent_type = state.get('agent_type') or payload.get('agent_type', 'subagent')
        _send_span(
            session_id=session_id,
            span_id_source=agent_id,
            name=agent_type,
            start_ns=state.get('start_ns', end_ns),
            end_ns=end_ns,
            span_input={'agent_type': agent_type},
            span_output=None,
            is_error=False,
            parent_span_id_source=state.get('prompt_id') or payload.get('prompt_id'),
        )
        return

    # ── Tool-Calls — Kind des Sub-Agenten-Spans (falls agent_id gesetzt) oder
    #    direkt des Turn-Spans ─────────────────────────────────────────────
    tool_use_id = payload.get('tool_use_id', '')
    if not tool_use_id:
        return

    if event == 'PreToolUse':
        _write_state(session_id, 'tool', tool_use_id, {
            'start_ns': time.time_ns(),
            'tool_name': payload.get('tool_name', ''),
            'tool_input': payload.get('tool_input'),
            'agent_id': payload.get('agent_id', ''),
            'prompt_id': payload.get('prompt_id', ''),
        })
        return

    if event in ('PostToolUse', 'PostToolUseFailure'):
        state = _read_and_clear_state(session_id, 'tool', tool_use_id)
        end_ns = time.time_ns()
        tool_name = state.get('tool_name') or payload.get('tool_name', 'unknown_tool')
        is_error = event == 'PostToolUseFailure'
        output = {'error': payload.get('error')} if is_error else payload.get('tool_response')
        parent_source = (
            state.get('agent_id') or payload.get('agent_id')
            or state.get('prompt_id') or payload.get('prompt_id')
        )

        _send_span(
            session_id=session_id,
            span_id_source=tool_use_id,
            name=tool_name,
            start_ns=state.get('start_ns', end_ns),
            end_ns=end_ns,
            span_input=state.get('tool_input'),
            span_output=output,
            is_error=is_error,
            parent_span_id_source=parent_source,
        )


if __name__ == '__main__':
    main()
