Option Explicit
Dim shell, fs, folder, candidate, launcher
Set shell = CreateObject("WScript.Shell")
Set fs = CreateObject("Scripting.FileSystemObject")
folder = fs.GetParentFolderName(WScript.ScriptFullName)
launcher = ""
For Each candidate In Array(".venv-314", ".venv-gui", ".venv")
  If fs.FileExists(folder & "\" & candidate & "\Scripts\shade-gui.exe") Then
    launcher = folder & "\" & candidate & "\Scripts\shade-gui.exe"
    Exit For
  End If
Next
If launcher = "" Then
  MsgBox "The SHADE desktop application has not been installed. See docs\DESKTOP.md.", 48, "SHADE"
Else
  shell.CurrentDirectory = folder
  shell.Run Chr(34) & launcher & Chr(34) & " --config " & Chr(34) & folder & "\config.toml" & Chr(34), 1, False
End If
