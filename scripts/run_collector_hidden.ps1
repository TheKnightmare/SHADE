# Runs the SHADE collector with its console window fully suppressed AND as a
# true child process of this script (not a job-breakaway launch), so that
# stopping the scheduled task actually stops the collector - not just this
# wrapper.
#
# History: an earlier version of this wrapper used a VBScript calling
# WScript.Shell.Run(cmd, 0, True) to hide shade.exe's console. That hid the
# window correctly, but WScript.Shell.Run creates its child through a path
# that does NOT stay inside the launching process's Job Object, so
# Stop-ScheduledTask (which terminates the task's Job Object) killed wscript
# .exe but left shade.exe running orphaned - confirmed directly: after a
# manual Stop-ScheduledTask, `Get-Process shade` still showed a live PID.
#
# Start-Process -NoNewWindow uses a direct CreateProcess call (UseShellExecute
# = $false) rather than ShellExecute/WMI, so the child stays in this
# PowerShell host's own Job Object - the same one Task Scheduler assigns to
# whatever it launches - and -WindowStyle Hidden on THIS host suppresses its
# own console, which shade.exe then shares (no separate window is ever
# created for it).
$root = Split-Path -Parent $PSScriptRoot
$exe = Join-Path $root '.venv-314\Scripts\shade.exe'
$config = Join-Path $root 'config.toml'
$proc = Start-Process -FilePath $exe -ArgumentList @('--config', $config, '--mode', 'standard', 'run-once') `
    -WorkingDirectory $root -NoNewWindow -Wait -PassThru
exit $proc.ExitCode
