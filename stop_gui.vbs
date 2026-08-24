' QQ Bot v3 - windowless stopper (double-click to stop)
Dim fso, sh, root
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
root = fso.GetParentFolderName(WScript.ScriptFullName)
If root = "" Then
    MsgBox "Cannot determine the script folder.", vbCritical
    WScript.Quit 1
End If
sh.CurrentDirectory = root
sh.Run "powershell -NoProfile -ExecutionPolicy Bypass -File stop_bot.ps1", 0, True
