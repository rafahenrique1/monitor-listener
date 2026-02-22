# Monitor Listener - Controle DDC/CI via MQTT

Controla a entrada do monitor (HDMI, DisplayPort, USB-C) via comandos MQTT, permitindo trocar de computador pelo Home Assistant ou Stream Deck.

## Arquitetura

```
┌─────────────┐     MQTT      ┌──────────────────┐     MQTT      ┌──────────────┐
│   Home       │◄────────────►│   Broker MQTT     │◄────────────►│   Home       │
│   Assistant  │              │   (Raspberry Pi)  │              │   Assistant  │
│   / Stream   │              │   192.168.68.63   │              │   / Stream   │
│   Deck       │              └──────────────────┘              │   Deck       │
└─────────────┘                 ▲            ▲                  └──────────────┘
                                │            │
                    ┌───────────┘            └───────────┐
                    │                                    │
              ┌─────┴──────┐                      ┌─────┴──────┐
              │   macOS     │                      │   Windows  │
              │   Listener  │                      │   Listener │
              │  (m1ddc)    │                      │  (dxva2)   │
              └────────────┘                      └────────────┘
```

- **macOS** escuta em `macbook/comando/monitor` e usa `m1ddc` para DDC/CI
- **Windows** escuta em `windows/comando/monitor` e usa a API nativa `dxva2.dll`

---

## Instalação no Windows

### Pré-requisitos

1. **Python 3.10 ou superior**
   - Baixe em [python.org/downloads](https://www.python.org/downloads/)
   - **IMPORTANTE:** marque a opção **"Add Python to PATH"** durante a instalação
   - Após instalar, confirme no Prompt de Comando:
     ```
     python --version
     pip --version
     ```

2. **Monitor com suporte a DDC/CI**
   - A maioria dos monitores modernos suporta DDC/CI
   - Verifique no menu OSD do monitor se a opção DDC/CI está habilitada
   - Monitores conectados por HDMI, DisplayPort ou USB-C geralmente funcionam

3. **Broker MQTT acessível na rede**
   - O broker precisa estar rodando (ex: Mosquitto no Home Assistant)
   - O PC Windows precisa estar na mesma rede (ou ter rota para `192.168.68.63`)

### Instalação automática (recomendado)

1. Clone ou baixe este repositório:
   ```
   git clone https://github.com/rafahenrique1/monitor-listener.git
   cd monitor-listener
   ```

2. Execute o instalador **como Administrador**:
   - Clique com botão direito em `install_startup.bat` → **"Executar como administrador"**
   - Ou no Prompt (Admin):
     ```
     install_startup.bat
     ```

3. Pronto! O listener:
   - Instala as dependências Python automaticamente
   - Cria uma tarefa agendada que inicia com o Windows
   - Inicia imediatamente em background

### Instalação manual

1. Instale as dependências:
   ```
   pip install -r requirements.txt
   ```

2. Teste o script manualmente:
   ```
   python monitor_listener_windows.py
   ```
   Deve aparecer no console:
   ```
   Monitor Listener WINDOWS iniciando (PID: ...)
   Monitores DDC/CI encontrados: 1
   Conectado ao Broker MQTT!
   Escutando topico: windows/comando/monitor
   ```

3. Para rodar em background sem janela:
   ```
   pythonw monitor_listener_windows.py
   ```
   Ou dê duplo clique no `start_hidden.vbs`.

---

## Comandos MQTT

Envie para o tópico `windows/comando/monitor` (ou `macbook/comando/monitor` no Mac):

| Comando | Ação |
|---|---|
| `ir_mac` ou `mac` | Troca para DisplayPort 1 (Mac) |
| `ir_windows` ou `windows` | Troca para HDMI 1 (Windows) |
| `hdmi1` | HDMI 1 (valor DDC: 17) |
| `hdmi2` | HDMI 2 (valor DDC: 18) |
| `dp1` | DisplayPort 1 (valor DDC: 15) |
| `dp2` | DisplayPort 2 (valor DDC: 16) |
| `usbc1` | USB-C 1 (valor DDC: 27) |
| `usbc2` | USB-C 2 (valor DDC: 28) |
| `bloquear` ou `lock` | Bloqueia a estação (Win+L) |
| `ping` | Responde `pong` no tópico de status |
| `status` | Publica timestamp no tópico de status |
| `detect_monitors` | Lista monitores DDC/CI encontrados |

---

## Configuração

Edite o início do `monitor_listener_windows.py` se necessário:

```python
BROKER = "192.168.68.63"  # IP do broker MQTT
PORT = 1883
USER = "hassa"
PASS = "Hassa1234"
```

Se o seu monitor está conectado em portas diferentes, ajuste o mapeamento nos comandos `ir_mac` e `ir_windows` dentro da função `on_message`.

---

## Tópicos de status

O listener publica automaticamente em `windows/status/monitor`:

- `online` — ao conectar
- `online:<timestamp>` — heartbeat a cada 30s
- `ok:input_<código>` — troca de input bem-sucedida
- `erro:...` — detalhes do erro
- `offline` — ao desconectar (via Will Message)

---

## Gerenciamento

| Ação | Comando |
|---|---|
| Parar o listener | `taskkill /F /IM pythonw.exe` |
| Ver se está rodando | `tasklist \| findstr pythonw` |
| Remover do startup | `schtasks /Delete /TN MonitorListenerMQTT /F` |
| Ver logs | Abra `%APPDATA%\monitor_listener\listener.log` |
| Reinstalar | Execute `install_startup.bat` novamente |

---

## Troubleshooting

**"Nenhum monitor fisico encontrado via DDC/CI"**
- Verifique se DDC/CI está habilitado no menu OSD do monitor
- Alguns monitores desabilitam DDC/CI por padrão
- Cabos HDMI/DP de baixa qualidade podem não passar DDC/CI

**"Erro de conexao"**
- Verifique se o broker MQTT está rodando
- Confirme que o IP `192.168.68.63` é acessível: `ping 192.168.68.63`
- Verifique as credenciais MQTT

**O monitor não troca de input**
- Rode `python monitor_listener_windows.py` no terminal para ver os logs
- Envie `detect_monitors` pelo MQTT para listar os monitores encontrados
- Tente valores DDC diferentes (alguns monitores usam valores não-padrão)

**pythonw.exe não encontrado**
- Reinstale o Python marcando "Add Python to PATH"
- Ou use o caminho completo: `C:\Users\SEU_USUARIO\AppData\Local\Programs\Python\Python3xx\pythonw.exe`
