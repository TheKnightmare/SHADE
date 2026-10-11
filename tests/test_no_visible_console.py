"""Regression check for the visible-console-window complaint.

Root cause, round 1 (identified in an earlier session): SHADE's own Python
source spawns no subprocess anywhere - the visible command shell was the
Windows Scheduled Task "SHADE Collector" itself, which ran a console-
subsystem `shade.exe` every 15 minutes with its Settings.Hidden flag left at
the Windows default (False). "Fixed" by setting that task's Hidden=True.

Root cause, round 2 (the window came back - confirmed this session): the
task's Principal uses LogonType=InteractiveToken ("run only when user is
logged on"), which runs the action on the user's real interactive desktop.
On that desktop, a console-subsystem process gets a new, visible console
window regardless of the task's Hidden setting - Hidden only hides the task
definition from the Task Scheduler UI, never the window of what it launches.
Manually invoking Start-ScheduledTask did not reliably reproduce this, which
is why it slipped past round-1's manual-trigger-only verification; the real
recurring TimeTrigger did reproduce it. Fixed for real this time by moving
the launch through scripts/run_collector_hidden.ps1, which starts shade.exe
via `Start-Process -NoNewWindow -Wait` - no console is ever allocated for it,
and (unlike an earlier VBScript-based wrapper tried and rejected in this
session, which fixed the window but orphaned shade.exe on Stop-ScheduledTask
because WScript.Shell.Run's child does not stay in the launching process's
Job Object) NoNewWindow keeps the child in the same Job Object Task
Scheduler assigns, so Stop-ScheduledTask actually stops collection.

Neither fix is a source-code change, so it can't be asserted by importing
shade_node - this test instead asserts two things that together make the
diagnosis and fix correct: (1) SHADE's own code never spawns a subprocess/
shell of its own, so any visible console window must come from how something
OUTSIDE this codebase invokes `shade.exe`; and (2) the wrapper script that
IS that outside launcher uses the no-console, job-preserving launch
mechanism rather than a window-style flag that (as proven this session)
doesn't actually suppress window creation for a freshly-launched console
process on an interactive desktop.
"""
import ast
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / 'src' / 'shade_node'
SCRIPTS = Path(__file__).resolve().parent.parent / 'scripts'
FORBIDDEN_MODULES = {'subprocess', 'os.system', 'os.startfile', 'os.spawnl', 'os.spawnv'}


class TestNoVisibleConsole(unittest.TestCase):
    # Reviewed exceptions: operator-pressed actions that spawn hidden (CREATE_NO_WINDOW), never a shell.
    WINDOWLESS_SPAWNERS = {'node_settings.py'}   # Restart NODE button -> node_service_control.ps1

    def test_no_module_spawns_a_subprocess_or_shell(self):
        offenders = []
        for path in SRC.glob('*.py'):
            if path.name in self.WINDOWLESS_SPAWNERS:
                text = path.read_text(encoding='utf-8')
                self.assertIn('CREATE_NO_WINDOW', text, f'{path.name} must spawn with CREATE_NO_WINDOW')
                self.assertNotIn('shell=True', text)
                continue
            tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name in FORBIDDEN_MODULES or alias.name.startswith('subprocess'):
                            offenders.append(f'{path.name}: import {alias.name}')
                if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith('subprocess'):
                    offenders.append(f'{path.name}: from {node.module} import ...')
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                    full = f'{node.value.id}.{node.attr}'
                    if full in FORBIDDEN_MODULES:
                        offenders.append(f'{path.name}: {full}(...)')
        self.assertEqual(offenders, [],
            'SHADE source spawns a subprocess/shell - this would need its own '
            'window-hiding treatment (creationflags=CREATE_NO_WINDOW or similar), '
            'not just the scheduled-task fix: ' + '; '.join(offenders))

    def test_scheduled_task_wrapper_uses_no_console_job_preserving_launch(self):
        wrapper = SCRIPTS / 'run_collector_hidden.ps1'
        self.assertTrue(wrapper.is_file(), f'{wrapper} is missing - this is the only thing standing '
                         'between the scheduled task and a visible console window on an interactive desktop')
        code_lines = [line for line in wrapper.read_text(encoding='utf-8').splitlines()
                      if not line.strip().startswith('#')]
        code = '\n'.join(code_lines)
        self.assertIn('-NoNewWindow', code,
            'wrapper must launch shade.exe with -NoNewWindow: this is what actually prevents a new '
            'console being allocated on an interactive-desktop LogonType, not a WindowStyle/Hidden flag')
        self.assertNotIn('WScript', code,
            "don't reintroduce the WScript.Shell.Run wrapper - its child breaks away from the "
            'launching process\'s Job Object, so Stop-ScheduledTask orphans shade.exe instead of stopping it')


if __name__ == '__main__':
    unittest.main(verbosity=2)
