# AC Bridge — Agent Connect

Verbindet deinen lokalen KI-Agenten mit dem Alexa Skill "Agent Connect" und der iOS App Agent Connect.

## Voraussetzungen

- Python 3.8+
- `python3-venv` (`sudo apt install python3-venv`)
- Laufender KI-Agent mit OpenAI-kompatiblem API-Endpunkt (z.B. OpenWebUI)
- Account auf https://agent-connect.computeq.de

## Installation

```bash
# 1. Repository klonen
git clone https://github.com/computeq-admin/ac-bridge.git
cd aat-bridge

# 2. Installation (erstellt venv, installiert Dependencies)
./install.sh

# 3. Einrichtung (interaktiv)
./start.sh --setup
```

## Einrichtung (setup.py)

Das Setup-Skript führt dich durch folgende Schritte:

1. **Token-A generieren** — eindeutige ID deiner Bridge-Installation
2. **Token-A im Webportal eintragen** → https://ai-agent-tasks.computeq.de/account.php
3. **OTT (One-Time-Token) eingeben** — vom Portal zurückgegeben
4. **Agent-Endpunkt konfigurieren** — URL + API-Token deines KI-Agenten
5. **MQTT konfigurieren** — Verbindungsdaten vom Portal

Am Ende wird `config.json` gespeichert (Berechtigungen: 600).

## Bridge starten

```bash
# Manuell
./start.sh

# Als Systemd Service
sudo cp aat_bridge.service /etc/systemd/system/
# YOUR_USER in der Service-Datei anpassen!
sudo nano /etc/systemd/system/aat_bridge.service
sudo systemctl enable --now aat_bridge

# Logs
journalctl -u aat_bridge -f
# oder
tail -f aat_bridge.log
```

## Konfiguration (config.json)

Wird von setup.py automatisch erstellt. Felder:

| Feld             | Beschreibung                                      |
|------------------|---------------------------------------------------|
| `token_a`        | Bridge-Identifikation (nicht ändern!)             |
| `token_b`        | Rotierender API-Token (wird automatisch erneuert) |
| `server_url`     | URL des AAT-Servers                               |
| `agent_endpoint` | OpenAI-kompatibler Endpunkt deines Agenten        |
| `agent_token`    | API-Token für den Agenten                         |
| `agent_model`    | Modell-Name (z.B. `chatcompletion`)               |
| `agent_timeout`  | Timeout in Sekunden (Standard: 120)               |
| `mqtt_host`      | MQTT Broker Host                                  |
| `mqtt_port`      | MQTT Broker Port (Standard: 1883)                 |
| `mqtt_user`      | MQTT Benutzername                                 |
| `mqtt_password`  | MQTT Passwort                                     |
| `mqtt_tls`       | TLS verwenden (true/false)                        |

## Ablauf

```
Alexa → AAT Server → MQTT Wakeup → Bridge
                                      ↓
                              Job von Server holen
                                      ↓
                              KI-Agent aufrufen
                                      ↓
                              Antwort an Server
                                      ↓
                         Optional: Telegram-Benachrichtigung
                                      ↓
                              Alexa liest Antwort vor
```

## Neu einrichten (Token zurücksetzen)

```bash
./start.sh --setup
# → wähle "Neu konfigurieren" → neues Token-A wird generiert
# → altes Token-A im Portal durch neues ersetzen
```
