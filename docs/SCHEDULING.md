# Windows scheduled collection (instructions only)

The release does not register, enable or modify a task. Review the local inbox
and doctor output before enabling unattended collection. Use the local venv,
not whichever older shade.exe happens to be on PATH.

Required action:

- Program: `C:\Station\SHADE\.venv\Scripts\shade.exe`
- Arguments: `--config "C:\Station\SHADE\config.toml" --mode standard run-once`
- Start in: `C:\Station\SHADE`
- Repeat: every 15 minutes; do not start a new instance while one is running.
- Run under the existing operator account with least privileges and network access.

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
$shadeAction = New-ScheduledTaskAction -Execute 'C:\Station\SHADE\.venv\Scripts\shade.exe' -Argument '--config "C:\Station\SHADE\config.toml" --mode standard run-once' -WorkingDirectory 'C:\Station\SHADE'
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
