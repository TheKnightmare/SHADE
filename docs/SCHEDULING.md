# Windows scheduled collection (instructions only)

The release does not register, enable or modify a task. Review the local inbox
and doctor output before enabling unattended collection. Use the local venv,
not whichever older shade.exe happens to be on PATH.

**Do not point the task's action directly at `shade.exe`.** `shade.exe` is a
console-subsystem executable, and with a `LogonType=InteractiveToken`
principal ("run only when user is logged on" - the normal desktop setup),
Task Scheduler runs the action on the user's real interactive desktop. A
console-subsystem process launched there gets a new, visible console window
- the task's own `Hidden` setting does NOT prevent this; `Hidden` only hides
the task definition from the Task Scheduler UI, not the window of whatever
it launches. This was confirmed as the root cause of a real recurring
visible-console-window regression, isolated by identifying the task's
`LogonType` and reproducing/eliminating the window purely by changing the
process-creation boundary, not by re-toggling `Hidden`.

Point the action at `scripts\run_collector_hidden.ps1` instead - it launches
`shade.exe` via `Start-Process -NoNewWindow -Wait`, which keeps the child in
the same Job Object Task Scheduler assigns to the action (so `Stop-
ScheduledTask` actually stops collection, not just the wrapper) and never
allocates it a console at all.

Required action:

- Program: `powershell.exe`
- Arguments: `-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "C:\Station\SHADE\scripts\run_collector_hidden.ps1"`
- Start in: `C:\Station\SHADE`
- Repeat: every 15 minutes; do not start a new instance while one is running.
- Run under the existing operator account with least privileges and network access.

(`run_collector_hidden.ps1` resolves the venv and config path itself, relative
to its own location - it does not need the config path passed in.)

The explicit STANDARD flag overrides a private configuration accidentally left
in ACTUAL. Never schedule ACTUAL. Hourly source cooldowns are respected even
when the task runs every 15 minutes. Errors and HTTP Retry-After extend cooldowns.

To update and re-enable an EXISTING task, first find its exact name:

```powershell
Get-ScheduledTask | Where-Object TaskName -Like '*SHADE*' | Select-Object TaskName,TaskPath,State
```

Then replace the example name/path below with that existing task's values:

```powershell
$shadeTaskName = 'SHADE Collector'
$shadeTaskPath = '\'
$shadeAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "C:\Station\SHADE\scripts\run_collector_hidden.ps1"' -WorkingDirectory 'C:\Station\SHADE'
$shadeTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 15)
$shadeSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
Set-ScheduledTask -TaskName $shadeTaskName -TaskPath $shadeTaskPath -Action $shadeAction -Trigger $shadeTrigger -Settings $shadeSettings
Enable-ScheduledTask -TaskName $shadeTaskName -TaskPath $shadeTaskPath
Get-ScheduledTaskInfo -TaskName $shadeTaskName -TaskPath $shadeTaskPath
```

These commands modify only the explicitly selected existing task and retain its
principal. If no matching task exists, create one manually in Task Scheduler
using the action/trigger above; do not assume a task name. Avoid overlap with any
old user-level collector. Task Scheduler result 0 indicates a completed command;
review source-level failures in interactive output as partial source failures
can still leave useful successful results.
