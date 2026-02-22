' start_hidden.vbs
' Inicia o monitor_listener_windows.py sem janela visível (background)
' Usado pelo Task Scheduler e pelo install_startup.bat

Dim scriptDir, pythonScript
scriptDir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
pythonScript = scriptDir & "\monitor_listener_windows.py"

Set shell = CreateObject("WScript.Shell")

' pythonw.exe roda sem console; python.exe como fallback
On Error Resume Next
shell.Run "pythonw.exe """ & pythonScript & """", 0, False
If Err.Number <> 0 Then
    Err.Clear
    shell.Run "python.exe """ & pythonScript & """", 0, False
End If
