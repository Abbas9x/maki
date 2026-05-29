' start_maki_hidden.vbs — V7.5 silent launcher with full diagnostic logging.
' Writes every step to projectmaki\logs\startup.log so we can see WHY a launch
' failed when Task Scheduler / Startup folder fires it.

Option Explicit

Dim WshShell, fso, projectDir, scriptPath, logPath, logDir
Set WshShell = CreateObject("WScript.Shell")
Set fso      = CreateObject("Scripting.FileSystemObject")

scriptPath = WScript.ScriptFullName
projectDir = Left(scriptPath, InStrRev(scriptPath, "\") - 1)
WshShell.CurrentDirectory = projectDir

logDir  = projectDir & "\logs"
If Not fso.FolderExists(logDir) Then fso.CreateFolder(logDir)
logPath = logDir & "\launcher.log"

Sub Log(msg)
    Dim f
    On Error Resume Next
    Set f = fso.OpenTextFile(logPath, 8, True)  ' append, create
    If Not (f Is Nothing) Then
        f.WriteLine Now & "  " & msg
        f.Close
    End If
    On Error Goto 0
End Sub

Log "========================================"
Log "VBS_STARTED         : " & scriptPath
Log "Project dir         : " & projectDir
Log "User                : " & WshShell.ExpandEnvironmentStrings("%USERNAME%")

' ── Singleton guard ──────────────────────────────────────────────────────────
Dim wmi, procs, p, alreadyRunning
alreadyRunning = False
On Error Resume Next
Set wmi = GetObject("winmgmts:\\.\root\cimv2")
Set procs = wmi.ExecQuery("SELECT CommandLine FROM Win32_Process WHERE Name='pythonw.exe' OR Name='python.exe'")
If Err.Number = 0 And Not (procs Is Nothing) Then
    For Each p In procs
        If Not IsNull(p.CommandLine) Then
            If InStr(LCase(p.CommandLine), "main.py") > 0 _
               And InStr(LCase(p.CommandLine), LCase(projectDir)) > 0 Then
                alreadyRunning = True
                Exit For
            End If
        End If
    Next
End If
On Error Goto 0

If alreadyRunning Then
    Log "DUPLICATE_BLOCKED   : Maki already running — exiting cleanly."
    WScript.Quit 0
End If

' ── Find a Python launcher (priority: venv → PATH) ───────────────────────────
Dim pyExe, candidates, i
candidates = Array( _
    projectDir & "\.venv\Scripts\pythonw.exe", _
    projectDir & "\.venv\Scripts\python.exe", _
    projectDir & "\venv\Scripts\pythonw.exe", _
    projectDir & "\venv\Scripts\python.exe" _
)

pyExe = ""
For i = 0 To UBound(candidates)
    If fso.FileExists(candidates(i)) Then
        ' Only use this venv if it actually has Maki's packages installed.
        ' We check for customtkinter (V7 UI dep) as a sentinel.
        Dim venvRoot, sentinel
        venvRoot = Left(candidates(i), InStrRev(candidates(i), "\Scripts\") - 1)
        sentinel = venvRoot & "\Lib\site-packages\customtkinter"
        If fso.FolderExists(sentinel) Then
            pyExe = candidates(i)
            Log "Found venv python (packages OK): " & pyExe
            Exit For
        Else
            Log "Skipping incomplete venv (missing customtkinter): " & venvRoot
        End If
    End If
Next

' Fall back to user-wide / system-wide installations.
If pyExe = "" Then
    ' Common system locations
    Dim sysCands
    sysCands = Array( _
        WshShell.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python314\pythonw.exe"), _
        WshShell.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"), _
        WshShell.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe"), _
        WshShell.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python310\pythonw.exe"), _
        "C:\Python314\pythonw.exe", _
        "C:\Python312\pythonw.exe", _
        "C:\Python311\pythonw.exe" _
    )
    For i = 0 To UBound(sysCands)
        If fso.FileExists(sysCands(i)) Then
            pyExe = sysCands(i)
            Log "Found system python: " & pyExe
            Exit For
        End If
    Next
End If

' Last resort: trust PATH (may fail at first-login boot)
If pyExe = "" Then
    pyExe = "pythonw.exe"
    Log "WARNING: no explicit python found — relying on PATH"
End If

' Sanity: pythonw.exe exists?
Dim pyExists
pyExists = fso.FileExists(pyExe)
Log "Python path exists  : " & pyExists & "  (" & pyExe & ")"

' ── Launch ───────────────────────────────────────────────────────────────────
Dim cmd, rc
cmd = """" & pyExe & """ main.py"
Log "RUN_COMMAND         : " & cmd

On Error Resume Next
rc = WshShell.Run(cmd, 0, False)   ' window style 0 = hidden, async — returns 0 on success-to-spawn
If Err.Number <> 0 Then
    Log "ERROR launching Maki: " & Err.Number & "  " & Err.Description
Else
    Log "WshShell_Run_called : returncode=" & rc
End If
On Error Goto 0

Log "VBS_DONE"
Set WshShell = Nothing
Set fso      = Nothing
