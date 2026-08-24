' QQ Bot v3 - windowless launcher (double-click to start)
' Pure ASCII + CRLF. No shell, no redirection.
' 08-24: pythonw.exe silents a crash (no console, no log) -> switched to
' python.exe (same engine start.bat uses, proven to work) with hidden window
' and startup log redirected to data\run\gui_start.log for diagnosability.
Dim fso, sh, root
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
root = fso.GetParentFolderName(WScript.ScriptFullName)
If root = "" Then
    MsgBox "Cannot determine the script folder." & vbCrLf & "Double-click start_gui.vbs from its own directory.", vbCritical
    WScript.Quit 1
End If
If fso.FolderExists(root & "\data") = False Then fso.CreateFolder root & "\data"
If fso.FolderExists(root & "\data\run") = False Then fso.CreateFolder root & "\data\run"
sh.CurrentDirectory = root
sh.Run "cmd /c ""cd /d """ & root & """ && python\python.exe gui_launcher.py > data\run\gui_start.log 2>&1""", 0, False