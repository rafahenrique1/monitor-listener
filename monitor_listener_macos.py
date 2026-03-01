#!/usr/bin/env python3
"""
Monitor Listener - Escuta comandos MQTT para controlar entrada do monitor
Solução para Macs corporativos onde SSH está bloqueado pelo MDM
"""

import paho.mqtt.client as mqtt
import subprocess
import time
import logging
import os
import signal
import sys
import struct
import threading
import socket as _socket_module
from datetime import datetime

TOPIC_WINDOWS_COMANDO = "windows/comando/monitor"

# --- CONFIGURAÇÕES ---
BROKER = "192.168.68.63"  # IP do seu Raspberry/Home Assistant
PORT = 1883
USER = "hassa"            # Usuário MQTT
PASS = "Hassa1234"        # Senha MQTT

# Tópicos MQTT
TOPIC_COMANDO = "macbook/comando/monitor"
TOPIC_STATUS = "macbook/status/monitor"

# Caminho do m1ddc (ajuste se necessário)
M1DDC_PATH = "/opt/homebrew/bin/m1ddc"

# Display a controlar: será auto-detectado se None.
# Se quiser forçar, use "1", "2", etc.
# Rode no Terminal: m1ddc display list   para ver os displays disponíveis.
M1DDC_DISPLAY = None  # Auto-detectar

# Mapeamento de inputs (DDC/CI values)
INPUTS = {
    "hdmi1": "17",      # HDMI 1
    "hdmi2": "18",      # HDMI 2
    "usbc1": "27",      # USB-C 1
    "usbc2": "28",      # USB-C 2
    "dp1": "15",        # DisplayPort 1
    "dp2": "16",        # DisplayPort 2
}

# Interface Wi-Fi do Mac (quase sempre en0)
WIFI_INTERFACE = "en0"

# macOS socket option para vincular socket a uma interface de rede específica
# Garante que a conexão MQTT use o Wi-Fi mesmo quando VPN está ativa
IP_BOUND_IF = 25  # IP_BOUND_IF definido em <netinet/in.h> do macOS

# PID file para evitar múltiplas instâncias
PID_FILE = os.path.expanduser("~/.local/bin/monitor_listener.pid")

# Configurar logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def install_interface_binding():
    """
    Monkey-patch socket.create_connection para forçar conexões MQTT
    a usar a interface Wi-Fi (en0), bypassing VPN.

    No macOS, a opção de socket IP_BOUND_IF vincula o socket a uma
    interface de rede específica, garantindo que o tráfego para o broker
    MQTT local não seja redirecionado pelo túnel VPN.

    Isso funciona automaticamente:
    - Com VPN: tráfego MQTT vai pelo Wi-Fi direto para a rede local
    - Sem VPN: tráfego MQTT vai pelo Wi-Fi normalmente
    - Reconexões também são vinculadas ao Wi-Fi
    """
    # Verifica se a interface Wi-Fi existe
    try:
        ifindex = _socket_module.if_nametoindex(WIFI_INTERFACE)
        logger.info(f"🛡️ Interface {WIFI_INTERFACE} encontrada (index={ifindex})")
    except OSError:
        logger.warning(f"⚠️ Interface {WIFI_INTERFACE} não encontrada. Binding desativado.")
        return

    # Verifica se a interface está ativa
    try:
        result = subprocess.run(
            ['ifconfig', WIFI_INTERFACE],
            capture_output=True, text=True, timeout=5
        )
        if 'status: active' not in result.stdout:
            logger.warning(f"⚠️ Interface {WIFI_INTERFACE} não está ativa. Binding desativado.")
            return
    except Exception:
        pass  # Se não conseguir verificar, tenta mesmo assim

    _original_create_connection = _socket_module.create_connection

    def _bound_create_connection(address, timeout=_socket_module._GLOBAL_DEFAULT_TIMEOUT,
                                 source_address=None, **kwargs):
        host, port = address
        is_mqtt_broker = (str(host) == BROKER and int(port) == PORT)

        # Para conexões que NÃO são ao broker MQTT, usa o caminho normal
        if not is_mqtt_broker:
            return _original_create_connection(address, timeout=timeout,
                                               source_address=source_address, **kwargs)

        # Para o broker MQTT, cria socket vinculado ao Wi-Fi
        err = None
        for res in _socket_module.getaddrinfo(host, port, 0, _socket_module.SOCK_STREAM):
            af, socktype, proto, canonname, sa = res
            sock = None
            try:
                sock = _socket_module.socket(af, socktype, proto)
                # Vincula ao Wi-Fi (bypassa VPN)
                try:
                    idx = _socket_module.if_nametoindex(WIFI_INTERFACE)
                    sock.setsockopt(_socket_module.IPPROTO_IP, IP_BOUND_IF,
                                    struct.pack('I', idx))
                except Exception as e:
                    logger.warning(f"setsockopt IP_BOUND_IF falhou: {e}")

                if timeout is not _socket_module._GLOBAL_DEFAULT_TIMEOUT:
                    sock.settimeout(timeout)
                if source_address:
                    sock.bind(source_address)
                sock.connect(sa)
                logger.info(f"🔗 Conexão MQTT vinculada ao {WIFI_INTERFACE} (bypassa VPN)")
                return sock
            except _socket_module.error as _err:
                err = _err
                if sock is not None:
                    sock.close()

        if err is not None:
            raise err
        raise _socket_module.error("getaddrinfo returns an empty list")

    _socket_module.create_connection = _bound_create_connection
    logger.info(f"🛡️ Interface binding ativo: conexões MQTT forçadas via {WIFI_INTERFACE}")


def check_single_instance():
    """Garante que só uma instância está rodando."""
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, 'r') as f:
                old_pid = int(f.read().strip())
            # Verifica se o processo antigo ainda está rodando
            os.kill(old_pid, 0)
            # Se chegou aqui, o processo existe - mata ele
            logger.warning(f"Instância anterior (PID {old_pid}) ainda rodando. Matando...")
            os.kill(old_pid, signal.SIGTERM)
            time.sleep(2)
            try:
                os.kill(old_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        except (ProcessLookupError, ValueError):
            pass  # Processo antigo já morreu
        except PermissionError:
            logger.error(f"Sem permissão para matar PID {old_pid}")

    # Grava nosso PID
    os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
    with open(PID_FILE, 'w') as f:
        f.write(str(os.getpid()))


def cleanup_pid():
    """Remove o PID file ao sair."""
    try:
        os.remove(PID_FILE)
    except FileNotFoundError:
        pass


def detect_external_display():
    """Auto-detecta o display externo.
    
    Nota: 'm1ddc display list' pode dar Segfault em algumas versões.
    Neste caso, tenta 'm1ddc get input' sem display para verificar se
    o default funciona.
    """
    # Primeiro tenta o 'display list' (pode dar segfault)
    try:
        result = subprocess.run(
            [M1DDC_PATH, "display", "list"],
            capture_output=True, text=True, timeout=10
        )
        output = (result.stdout or "").strip()
        
        if result.returncode != 0 or not output:
            logger.warning(f"m1ddc display list falhou (rc={result.returncode}). "
                          f"Usando modo sem display específico.")
        else:
            logger.info(f"m1ddc display list output:\n{output}")
            lines = output.strip().split('\n')
            for line in lines:
                line = line.strip()
                if line and line[0].isdigit():
                    display_num = line.split(':')[0].strip().split()[0]
                    logger.info(f"Display externo detectado: {display_num}")
                    return display_num
    except Exception as e:
        logger.warning(f"m1ddc display list erro/segfault: {e}. Usando modo sem display.")

    # Verifica se m1ddc funciona sem especificar display
    try:
        result = subprocess.run(
            [M1DDC_PATH, "get", "input"],
            capture_output=True, text=True, timeout=10
        )
        output = (result.stdout or "").strip()
        if result.returncode == 0 and output:
            logger.info(f"m1ddc sem display funciona! Input atual: {output}")
            return None  # None = usar sem display (modo default)
        else:
            logger.warning(f"m1ddc get input falhou (rc={result.returncode})")
    except Exception as e:
        logger.warning(f"m1ddc get input erro: {e}")

    return None


def bloquear_mac(client: mqtt.Client) -> bool:
    """Bloqueia a tela do Mac (macOS moderno - Tahoe compatível)"""
    try:
        logger.info("🔒 Bloqueando Mac...")
        
        # Método 1: ScreenSaver (funciona sem permissões especiais)
        # Se "Exigir senha imediatamente" estiver ativo, bloqueia ao mexer
        result = subprocess.run(
            ["open", "-a", "ScreenSaverEngine"],
            capture_output=True,
            text=True,
            timeout=5
        )
        
        if result.returncode == 0:
            logger.info("✅ ScreenSaver iniciado! (Mac bloqueado)")
            client.publish(TOPIC_STATUS, "ok:bloqueado")
            return True
        else:
            logger.warning(f"ScreenSaver falhou: {result.stderr}")
            # Fallback: pmset displaysleepnow
            logger.info("Tentando pmset displaysleepnow...")
            subprocess.run(["/usr/bin/pmset", "displaysleepnow"], timeout=5)
            logger.info("✅ Tela desligada (bloqueio via pmset)")
            client.publish(TOPIC_STATUS, "ok:bloqueado_pmset")
            return True
            
    except Exception as e:
        logger.error(f"❌ Erro ao bloquear: {e}")
        client.publish(TOPIC_STATUS, f"erro:bloqueio:{str(e)}")
        return False


def trocar_input(input_code: str, client: mqtt.Client, display_id: str = None) -> bool:
    """Executa o m1ddc para trocar a entrada do monitor (no display externo).
    
    Tenta múltiplas estratégias:
    1. Com display_id específico (se fornecido)
    2. Sem especificar display (usa default do m1ddc)
    3. Com display "1" como fallback
    """
    strategies = []
    if display_id:
        strategies.append(("display específico", [M1DDC_PATH, "display", display_id, "set", "input", input_code]))
    strategies.append(("default (sem display)", [M1DDC_PATH, "set", "input", input_code]))
    # Fallback: tenta display 1 se não era a estratégia principal
    if display_id != "1":
        strategies.append(("fallback display 1", [M1DDC_PATH, "display", "1", "set", "input", input_code]))

    for strategy_name, cmd in strategies:
        try:
            logger.info(f"Tentando ({strategy_name}): {' '.join(cmd)}")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10
            )
            
            stdout_out = (result.stdout or "").strip()
            stderr_out = (result.stderr or "").strip()
            logger.info(f"  returncode={result.returncode}, stdout='{stdout_out}', stderr='{stderr_out}'")
            
            if result.returncode == 0:
                logger.info(f"✅ Monitor trocado para input {input_code} (via {strategy_name})")
                client.publish(TOPIC_STATUS, f"ok:input_{input_code}")
                return True
            else:
                logger.warning(f"⚠️ Falhou ({strategy_name}): rc={result.returncode}")
                
        except subprocess.TimeoutExpired:
            logger.warning(f"⚠️ Timeout ({strategy_name})")
        except FileNotFoundError:
            logger.error(f"❌ m1ddc não encontrado em {M1DDC_PATH}")
            client.publish(TOPIC_STATUS, "erro:m1ddc_not_found")
            return False
        except Exception as e:
            logger.warning(f"⚠️ Erro ({strategy_name}): {e}")

    logger.error(f"❌ Todas as estratégias falharam para input {input_code}")
    client.publish(TOPIC_STATUS, f"erro:todas_estrategias_falharam")
    return False


def on_connect(client, userdata, flags, reason_code, properties=None):
    """Callback quando conecta ao broker MQTT"""
    if reason_code == 0 or str(reason_code) == "Success":
        logger.info("🟢 Conectado ao Broker MQTT!")
        client.subscribe(TOPIC_COMANDO)
        client.publish(TOPIC_STATUS, "online")
        logger.info(f"📡 Escutando tópico: {TOPIC_COMANDO}")
    else:
        logger.error(f"🔴 Falha na conexão. Código: {reason_code}")


def on_disconnect(client, userdata, flags, reason_code, properties=None):
    """Callback quando desconecta do broker"""
    logger.warning(f"🟡 Desconectado do Broker. Código: {reason_code}")


def on_message(client, userdata, msg):
    """Callback quando recebe mensagem MQTT"""
    payload = msg.payload.decode().strip().lower()
    logger.info(f"📨 Recebido: '{payload}' no tópico '{msg.topic}'")
    
    # Pega o display_id detectado do userdata
    display_id = userdata.get("display_id") if userdata else None
    
    # Comandos principais
    if payload == "ir_windows" or payload == "windows":
        logger.info("🖥️ Trocando monitor para Windows (HDMI)...")
        trocar_input("17", client, display_id)  # HDMI 1
        # Reforço: manda extend para o Windows em rajada para garantir que estenda as telas
        def _reforco_extend():
            for delay in (2, 4, 6):
                time.sleep(delay if delay == 2 else 2)
                client.publish(TOPIC_WINDOWS_COMANDO, "extend")
                logger.info(f"📡 Reforço extend enviado para Windows ({delay}s)")
        threading.Thread(target=_reforco_extend, daemon=True).start()
        
    elif payload == "ir_mac" or payload == "mac":
        logger.info("🍎 Trocando monitor para Mac (DisplayPort)...")
        trocar_input("15", client, display_id)  # DisplayPort 1
        # Reforço: re-tenta DDC + re-detecta display (monitor pode demorar a responder)
        def _reforco_mac():
            for i in range(3):
                time.sleep(3)
                logger.info(f"📡 Reforço DDC ir_mac ({(i+1)*3}s)")
                new_display = detect_external_display()
                if userdata and new_display:
                    userdata["display_id"] = new_display
                trocar_input("15", client, new_display or display_id)
        threading.Thread(target=_reforco_mac, daemon=True).start()
        
    elif payload == "ir_hdmi1" or payload == "hdmi1":
        trocar_input("17", client, display_id)
        
    elif payload == "ir_hdmi2" or payload == "hdmi2":
        trocar_input("18", client, display_id)
        
    elif payload == "ir_usbc1" or payload == "usbc1":
        trocar_input("27", client, display_id)
        
    elif payload == "ir_usbc2" or payload == "usbc2":
        trocar_input("28", client, display_id)
        
    elif payload == "ir_dp1" or payload == "dp1":
        trocar_input("15", client, display_id)
        
    elif payload == "ir_dp2" or payload == "dp2":
        trocar_input("16", client, display_id)
        
    elif payload == "bloquear" or payload == "lock":
        bloquear_mac(client)
        
    elif payload == "ping":
        client.publish(TOPIC_STATUS, "pong")
        logger.info("🏓 Pong!")
        
    elif payload == "status":
        client.publish(TOPIC_STATUS, f"online:{datetime.now().isoformat()}")
    
    elif payload == "detect_display":
        # Comando para re-detectar o display
        new_display = detect_external_display()
        if userdata:
            userdata["display_id"] = new_display or M1DDC_DISPLAY
        client.publish(TOPIC_STATUS, f"display:{new_display or 'default'}")
        logger.info(f"🔄 Display re-detectado: {new_display or 'default'}")
        
    else:
        logger.warning(f"⚠️ Comando desconhecido: {payload}")
        client.publish(TOPIC_STATUS, f"erro:comando_desconhecido:{payload}")


def main():
    """Loop principal"""
    # Garante instância única
    check_single_instance()
    
    logger.info("=" * 50)
    logger.info(f"🚀 Monitor Listener iniciando (PID: {os.getpid()})...")
    logger.info(f"   Broker: {BROKER}:{PORT}")
    logger.info(f"   Tópico: {TOPIC_COMANDO}")
    logger.info("=" * 50)
    
    # Instala binding de interface para bypasear VPN
    # Força conexões MQTT a usar Wi-Fi (en0) direto, sem passar pelo túnel VPN
    install_interface_binding()
    
    # Auto-detecta display externo
    display_id = M1DDC_DISPLAY
    if not display_id:
        display_id = detect_external_display()
        if display_id:
            logger.info(f"🖥️ Display externo detectado: {display_id}")
        else:
            logger.warning("⚠️ Nenhum display externo detectado. Tentará todas estratégias ao trocar input.")
    else:
        logger.info(f"🖥️ Display configurado manualmente: {display_id}")
    
    # Userdata para compartilhar estado com callbacks
    userdata = {"display_id": display_id}
    
    # Usa CallbackAPIVersion.VERSION2 para evitar DeprecationWarning
    try:
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            userdata=userdata
        )
    except (AttributeError, TypeError):
        # Fallback para versões mais antigas do paho-mqtt
        logger.warning("paho-mqtt antigo detectado, usando API v1")
        client = mqtt.Client(userdata=userdata)
    
    client.username_pw_set(USER, PASS)
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    
    # Will message - avisa se desconectar inesperadamente
    client.will_set(TOPIC_STATUS, "offline", qos=1, retain=True)
    
    def signal_handler(sig, frame):
        logger.info(f"👋 Recebido sinal {sig}. Encerrando...")
        try:
            client.publish(TOPIC_STATUS, "offline")
            client.disconnect()
        except Exception:
            pass
        cleanup_pid()
        sys.exit(0)
    
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    while True:
        try:
            logger.info(f"🔌 Conectando ao broker {BROKER}:{PORT}...")
            client.connect(BROKER, PORT, keepalive=60)
            client.loop_forever()
        except KeyboardInterrupt:
            logger.info("👋 Encerrando por comando do usuário...")
            try:
                client.publish(TOPIC_STATUS, "offline")
                client.disconnect()
            except Exception:
                pass
            break
        except Exception as e:
            logger.error(f"❌ Erro de conexão: {e}")
            logger.info("⏳ Tentando reconectar em 10 segundos...")
            time.sleep(10)  # Intervalo maior para não lotar o log
    
    cleanup_pid()


if __name__ == "__main__":
    main()
