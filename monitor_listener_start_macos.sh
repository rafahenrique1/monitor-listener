#!/bin/bash
# Wrapper para iniciar o Monitor Listener.
# O próprio Python já cuida de reconexão ao broker MQTT.

SCRIPT="/Users/rabpereira/.local/bin/monitor_listener.py"
LOG="/Users/rabpereira/Library/Logs/monitor-listener.log"

# Mata instâncias anteriores do Python rodando o mesmo script
pkill -f "python3.*monitor_listener.py" 2>/dev/null
sleep 1

# Aguarda 10s para dar tempo da rede subir no boot
sleep 10

echo "$(date '+%Y-%m-%d %H:%M:%S') - [PID $$] Iniciando monitor_listener.py..." >> "$LOG"
exec /usr/bin/python3 "$SCRIPT"
