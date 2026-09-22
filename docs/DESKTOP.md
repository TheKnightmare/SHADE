# SHADE desktop console

On the configured station, double-click **Open SHADE.vbs** in the SHADE folder,
or **SHADE** in `C:\Station\Launch`. No terminal is needed for daily use.

1. **Collect now** checks public feeds once. Source polling intervals still apply.
2. **Inbox** shows operational reports. **Top news** isolates fresh national/world
   context for a local-impact check. **NOW** shows current local life-safety weather.
3. Select a report to inspect its evidence. **Source links** opens original sources.
4. **Start review** enables **Prepare message**. Read the preview, then copy or save it.
5. **TX candidate** records a reviewed candidate. **Record as sent** only records a
   message you already sent yourself; it performs no transmission.

Search and filter by area, category, review state, age or score. **All evidence**
with **Include suppressed** reveals retained low-priority reports. Choose an
explicit state to inspect expired, rejected or sent reports. **Sources & health**
shows the most recent collection results and cooldowns.

**Housekeeping** previews state changes. Applying the preview backs up the database
and preserves every original observation. A changed preview must be reopened.

The console opens in STANDARD. EXERCISE marks messages at both ends. ACTUAL requires
an intentional mode choice and separate confirmation before each prepared message.
The local view refreshes every minute while idle; collection runs only on demand.

## Install on another Windows computer

Use Python 3.11 or newer with Tcl/Tk installed (the standard Windows Python installer
includes it). Preserve your private configuration and database when upgrading.
One-time setup from the extracted SHADE folder:

```powershell
py -m venv .venv-gui
.\.venv-gui\Scripts\python.exe -m pip install .\release\shade_node-0.4.0-py3-none-any.whl
```

Then double-click **Open SHADE.vbs**. It prefers `.venv-314`, then `.venv-gui`, then
`.venv`. If no config exists, the application asks you to select an existing station
configuration. Initial station setup is described in the README. The package includes
the SHADE icon and needs no additional GUI libraries.
