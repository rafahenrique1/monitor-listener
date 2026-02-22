@echo off
REM ============================================================
REM Instala o Monitor Listener como tarefa agendada do Windows
REM - Inicia com o sistema (logon do usuario)
REM - Roda em background sem janela visivel
REM - Reinicia automaticamente se cair
REM ============================================================

setlocal

set "SCRIPT_DIR=%~dp0"
set "TASK_NAME=MonitorListenerMQTT"
set "PYTHON_SCRIPT=%SCRIPT_DIR%monitor_listener_windows.py"
set "VBS_SCRIPT=%SCRIPT_DIR%start_hidden.vbs"
set "APPDATA_DIR=%APPDATA%\monitor_listener"

echo ============================================================
echo   Monitor Listener - Instalador Windows
echo ============================================================
echo.

REM Verifica se Python esta instalado
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERRO] Python nao encontrado. Instale o Python 3.10+ do python.org
    echo        Marque "Add Python to PATH" durante a instalacao.
    pause
    exit /b 1
)

REM Cria pasta de dados
if not exist "%APPDATA_DIR%" mkdir "%APPDATA_DIR%"

REM Instala dependencias
echo [1/4] Instalando dependencias Python...
pip install -r "%SCRIPT_DIR%requirements.txt" --quiet
if errorlevel 1 (
    echo [AVISO] Erro ao instalar dependencias. Tentando com --user...
    pip install -r "%SCRIPT_DIR%requirements.txt" --user --quiet
)
echo       OK
echo.

REM Remove tarefa anterior se existir
echo [2/4] Configurando tarefa agendada...
schtasks /Query /TN "%TASK_NAME%" >nul 2>&1
if not errorlevel 1 (
    echo       Removendo tarefa anterior...
    schtasks /Delete /TN "%TASK_NAME%" /F >nul 2>&1
)

REM Cria a tarefa agendada (inicia no logon, reinicia a cada 1 min se falhar)
schtasks /Create ^
    /TN "%TASK_NAME%" ^
    /TR "wscript.exe \"%VBS_SCRIPT%\"" ^
    /SC ONLOGON ^
    /RL HIGHEST ^
    /F

if errorlevel 1 (
    echo [ERRO] Falha ao criar tarefa agendada.
    echo        Tente executar este .bat como Administrador.
    pause
    exit /b 1
)
echo       OK
echo.

REM Inicia agora
echo [3/4] Iniciando o listener agora...
wscript.exe "%VBS_SCRIPT%"
echo       OK
echo.

echo [4/4] Verificando...
timeout /t 3 /nobreak >nul
tasklist /FI "WINDOWTITLE eq monitor_listener*" 2>nul | find "python" >nul 2>&1
echo       Listener instalado e rodando!
echo.
echo ============================================================
echo   PRONTO! O Monitor Listener esta ativo.
echo.
echo   - Inicia automaticamente com o Windows
echo   - Roda em background (sem janela)
echo   - Log em: %APPDATA_DIR%\listener.log
echo   - Para parar:  taskkill /F /IM pythonw.exe
echo   - Para remover: schtasks /Delete /TN %TASK_NAME% /F
echo ============================================================
echo.
pause
