#!/usr/bin/env python3
"""
Monitor Listener (Windows) - Escuta comandos MQTT para controlar entrada do monitor via DDC/CI
Versão Windows do monitor_listener.py do macOS

Usa a API nativa do Windows (dxva2.dll) para controlar monitores via DDC/CI,
sem necessidade de ferramentas externas.
"""

import paho.mqtt.client as mqtt
import ctypes
import ctypes.wintypes
import subprocess
import time
import logging
import os
import sys
import threading
from datetime import datetime

# --- CONFIGURAÇÕES ---
BROKER = "192.168.68.63"
PORT = 1883
USER = "hassa"
PASS = "Hassa1234"

# Tópicos MQTT (separados do Mac para evitar conflito)
TOPIC_COMANDO = "windows/comando/monitor"
TOPIC_STATUS = "windows/status/monitor"

# VCP code para Input Source Select (padrão DDC/CI)
VCP_INPUT_SOURCE = 0x60

# Mapeamento de inputs (DDC/CI values - mesmos do Mac)
INPUTS = {
    "hdmi1": 17,
    "hdmi2": 18,
    "usbc1": 27,
    "usbc2": 28,
    "dp1": 15,
    "dp2": 16,
}

# Pasta de dados e logs
APP_DATA_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "monitor_listener")
os.makedirs(APP_DATA_DIR, exist_ok=True)

LOCK_FILE = os.path.join(APP_DATA_DIR, "listener.lock")
LOG_FILE = os.path.join(APP_DATA_DIR, "listener.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Windows DDC/CI via dxva2.dll
# ---------------------------------------------------------------------------

class PHYSICAL_MONITOR(ctypes.Structure):
    _fields_ = [
        ("hPhysicalMonitor", ctypes.wintypes.HANDLE),
        ("szPhysicalMonitorDescription", ctypes.wintypes.WCHAR * 128),
    ]


_user32 = ctypes.windll.user32
_dxva2 = ctypes.windll.dxva2

_EnumDisplayMonitors = _user32.EnumDisplayMonitors
_GetPhysicalMonitorsFromHMONITOR = _dxva2.GetPhysicalMonitorsFromHMONITOR
_GetNumberOfPhysicalMonitorsFromHMONITOR = _dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR
_SetVCPFeature = _dxva2.SetVCPFeature
_GetVCPFeatureAndVCPFeatureReply = _dxva2.GetVCPFeatureAndVCPFeatureReply
_DestroyPhysicalMonitor = _dxva2.DestroyPhysicalMonitor

MONITORENUMPROC = ctypes.WINFUNCTYPE(
    ctypes.c_int,
    ctypes.wintypes.HMONITOR,
    ctypes.wintypes.HDC,
    ctypes.POINTER(ctypes.wintypes.RECT),
    ctypes.wintypes.LPARAM,
)


def _get_physical_monitors():
    """Retorna lista de (hMonitor_lógico, PHYSICAL_MONITOR) para cada monitor."""
    monitors = []

    def _callback(hMonitor, hdcMonitor, lprcMonitor, dwData):
        count = ctypes.wintypes.DWORD()
        if _GetNumberOfPhysicalMonitorsFromHMONITOR(hMonitor, ctypes.byref(count)):
            arr = (PHYSICAL_MONITOR * count.value)()
            if _GetPhysicalMonitorsFromHMONITOR(hMonitor, count.value, arr):
                for pm in arr:
                    monitors.append((hMonitor, pm))
        return 1  # continuar enumeração

    _EnumDisplayMonitors(None, None, MONITORENUMPROC(_callback), 0)
    return monitors


def _destroy_physical_monitor(pm: PHYSICAL_MONITOR):
    _DestroyPhysicalMonitor(pm.hPhysicalMonitor)


def get_current_input(pm: PHYSICAL_MONITOR):
    """Lê o input atual do monitor via DDC/CI."""
    cur = ctypes.wintypes.DWORD()
    max_val = ctypes.wintypes.DWORD()
    pvct = ctypes.wintypes.DWORD()
    ok = _GetVCPFeatureAndVCPFeatureReply(
        pm.hPhysicalMonitor,
        ctypes.wintypes.BYTE(VCP_INPUT_SOURCE),
        ctypes.byref(pvct),
        ctypes.byref(cur),
        ctypes.byref(max_val),
    )
    if ok:
        return cur.value
    return None


def set_monitor_input(input_code: int, monitor_index: int = None) -> bool:
    """
    Troca a entrada do monitor via DDC/CI.
    monitor_index: None = tenta todos, 0 = primeiro, 1 = segundo, etc.
    """
    monitors = _get_physical_monitors()
    if not monitors:
        logger.error("Nenhum monitor fisico encontrado via DDC/CI")
        return False

    logger.info(f"Monitores encontrados: {len(monitors)}")
    success = False

    targets = [monitors[monitor_index]] if monitor_index is not None and monitor_index < len(monitors) else monitors

    for i, (hMon, pm) in enumerate(targets):
        desc = pm.szPhysicalMonitorDescription or f"Monitor {i}"
        try:
            cur = get_current_input(pm)
            logger.info(f"  [{i}] {desc} - input atual: {cur}")

            ok = _SetVCPFeature(
                pm.hPhysicalMonitor,
                ctypes.wintypes.BYTE(VCP_INPUT_SOURCE),
                ctypes.wintypes.DWORD(input_code),
            )
            if ok:
                logger.info(f"  [{i}] {desc} - input alterado para {input_code}")
                success = True
            else:
                err = ctypes.GetLastError()
                logger.warning(f"  [{i}] {desc} - SetVCPFeature falhou (erro={err})")
        except Exception as e:
            logger.warning(f"  [{i}] {desc} - erro: {e}")
        finally:
            _destroy_physical_monitor(pm)

    for i, (hMon, pm) in enumerate(monitors):
        if (hMon, pm) not in targets:
            _destroy_physical_monitor(pm)

    return success


# ---------------------------------------------------------------------------
# Controle de instância única (sem sinais POSIX)
# ---------------------------------------------------------------------------

def check_single_instance():
    """Garante que só uma instância está rodando via lock file."""
    lock_dir = os.path.dirname(LOCK_FILE)
    os.makedirs(lock_dir, exist_ok=True)

    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, "r") as f:
                old_pid = int(f.read().strip())
            # Verifica se o processo ainda existe
            import psutil
            if psutil.pid_exists(old_pid):
                proc = psutil.Process(old_pid)
                if "python" in proc.name().lower():
                    logger.warning(f"Instancia anterior (PID {old_pid}) encontrada. Matando...")
                    proc.terminate()
                    proc.wait(timeout=5)
        except ImportError:
            # Sem psutil, tenta taskkill
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(old_pid), "/F"],
                    capture_output=True, timeout=5,
                )
            except Exception:
                pass
        except Exception:
            pass

    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))


def cleanup_lock():
    try:
        os.remove(LOCK_FILE)
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Ações
# ---------------------------------------------------------------------------

def bloquear_windows(client: mqtt.Client) -> bool:
    """Bloqueia a estação Windows."""
    try:
        logger.info("Bloqueando Windows...")
        ctypes.windll.user32.LockWorkStation()
        logger.info("Windows bloqueado")
        client.publish(TOPIC_STATUS, "ok:bloqueado")
        return True
    except Exception as e:
        logger.error(f"Erro ao bloquear: {e}")
        client.publish(TOPIC_STATUS, f"erro:bloqueio:{e}")
        return False


def forcar_estender_telas(client: mqtt.Client) -> bool:
    """Força modo estendido nos monitores (resolve espelhamento após KVM switch)."""
    try:
        logger.info("Forcando modo estendido (displayswitch /extend)...")
        result = subprocess.run(
            ["displayswitch.exe", "/extend"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            logger.info("Modo estendido aplicado")
            client.publish(TOPIC_STATUS, "ok:extend")
            return True
        else:
            logger.warning(f"displayswitch retornou {result.returncode}: {result.stderr}")
            client.publish(TOPIC_STATUS, f"erro:extend:rc{result.returncode}")
            return False
    except Exception as e:
        logger.error(f"Erro ao forcar extend: {e}")
        client.publish(TOPIC_STATUS, f"erro:extend:{e}")
        return False


def trocar_input(input_code: int, client: mqtt.Client, monitor_index: int = None) -> bool:
    """Troca o input e publica resultado via MQTT."""
    ok = set_monitor_input(input_code, monitor_index)
    if ok:
        client.publish(TOPIC_STATUS, f"ok:input_{input_code}")
    else:
        client.publish(TOPIC_STATUS, f"erro:input_{input_code}")
    return ok


# ---------------------------------------------------------------------------
# Callbacks MQTT
# ---------------------------------------------------------------------------

def on_connect(client, userdata, flags, reason_code, properties=None):
    if reason_code == 0 or str(reason_code) == "Success":
        logger.info("Conectado ao Broker MQTT!")
        client.subscribe(TOPIC_COMANDO)
        client.publish(TOPIC_STATUS, "online")
        logger.info(f"Escutando topico: {TOPIC_COMANDO}")
    else:
        logger.error(f"Falha na conexao. Codigo: {reason_code}")


def on_disconnect(client, userdata, flags, reason_code, properties=None):
    logger.warning(f"Desconectado do Broker. Codigo: {reason_code}")


def on_message(client, userdata, msg):
    payload = msg.payload.decode().strip().lower()
    logger.info(f"Recebido: '{payload}' no topico '{msg.topic}'")

    monitor_idx = userdata.get("monitor_index") if userdata else None

    if payload in ("ir_windows", "windows"):
        logger.info("Trocando monitor para Windows (HDMI)...")
        trocar_input(INPUTS["hdmi1"], client, monitor_idx)

    elif payload in ("ir_mac", "mac"):
        logger.info("Trocando monitor para Mac (DisplayPort)...")
        trocar_input(INPUTS["dp1"], client, monitor_idx)

    elif payload in ("ir_hdmi1", "hdmi1"):
        trocar_input(INPUTS["hdmi1"], client, monitor_idx)
    elif payload in ("ir_hdmi2", "hdmi2"):
        trocar_input(INPUTS["hdmi2"], client, monitor_idx)
    elif payload in ("ir_usbc1", "usbc1"):
        trocar_input(INPUTS["usbc1"], client, monitor_idx)
    elif payload in ("ir_usbc2", "usbc2"):
        trocar_input(INPUTS["usbc2"], client, monitor_idx)
    elif payload in ("ir_dp1", "dp1"):
        trocar_input(INPUTS["dp1"], client, monitor_idx)
    elif payload in ("ir_dp2", "dp2"):
        trocar_input(INPUTS["dp2"], client, monitor_idx)

    elif payload in ("bloquear", "lock"):
        bloquear_windows(client)

    elif payload in ("extend", "estender"):
        forcar_estender_telas(client)

    elif payload == "ping":
        client.publish(TOPIC_STATUS, "pong")
        logger.info("Pong!")

    elif payload == "status":
        client.publish(TOPIC_STATUS, f"online:{datetime.now().isoformat()}")

    elif payload == "detect_monitors":
        monitors = _get_physical_monitors()
        info = []
        for i, (hMon, pm) in enumerate(monitors):
            cur = get_current_input(pm)
            desc = pm.szPhysicalMonitorDescription or f"Monitor {i}"
            info.append(f"[{i}] {desc} input={cur}")
            _destroy_physical_monitor(pm)
        result = "; ".join(info) if info else "nenhum"
        client.publish(TOPIC_STATUS, f"monitors:{result}")
        logger.info(f"Monitores detectados: {result}")

    else:
        logger.warning(f"Comando desconhecido: {payload}")
        client.publish(TOPIC_STATUS, f"erro:comando_desconhecido:{payload}")


# ---------------------------------------------------------------------------
# Heartbeat - publica status periódico para o HA saber que está online
# ---------------------------------------------------------------------------

def heartbeat_loop(client: mqtt.Client, interval: int = 30):
    """Publica 'online' periodicamente para o Home Assistant."""
    while True:
        try:
            if client.is_connected():
                client.publish(TOPIC_STATUS, f"online:{datetime.now().isoformat()}")
        except Exception:
            pass
        time.sleep(interval)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    check_single_instance()

    logger.info("=" * 50)
    logger.info(f"Monitor Listener WINDOWS iniciando (PID: {os.getpid()})...")
    logger.info(f"   Broker: {BROKER}:{PORT}")
    logger.info(f"   Topico: {TOPIC_COMANDO}")
    logger.info("=" * 50)

    # Testa DDC/CI no boot
    monitors = _get_physical_monitors()
    logger.info(f"Monitores DDC/CI encontrados: {len(monitors)}")
    for i, (hMon, pm) in enumerate(monitors):
        cur = get_current_input(pm)
        desc = pm.szPhysicalMonitorDescription or f"Monitor {i}"
        logger.info(f"  [{i}] {desc} - input atual: {cur}")
        _destroy_physical_monitor(pm)

    userdata = {"monitor_index": None}

    try:
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            userdata=userdata,
        )
    except (AttributeError, TypeError):
        logger.warning("paho-mqtt antigo detectado, usando API v1")
        client = mqtt.Client(userdata=userdata)

    client.username_pw_set(USER, PASS)
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    client.will_set(TOPIC_STATUS, "offline", qos=1, retain=True)

    # Heartbeat em thread separada
    hb = threading.Thread(target=heartbeat_loop, args=(client, 30), daemon=True)
    hb.start()

    while True:
        try:
            logger.info(f"Conectando ao broker {BROKER}:{PORT}...")
            client.connect(BROKER, PORT, keepalive=60)
            client.loop_forever()
        except KeyboardInterrupt:
            logger.info("Encerrando por comando do usuario...")
            try:
                client.publish(TOPIC_STATUS, "offline")
                client.disconnect()
            except Exception:
                pass
            break
        except Exception as e:
            logger.error(f"Erro de conexao: {e}")
            logger.info("Tentando reconectar em 10 segundos...")
            time.sleep(10)

    cleanup_lock()


if __name__ == "__main__":
    main()
