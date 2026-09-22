# Upgrade to SHADE 0.4.0 on Windows

Use the wheel from the verified release. Keep the existing private config.toml
and data/shade.db. Stop or disable any existing collector task while upgrading;
do not delete its configuration or evidence. Upgrading from v0.3.1 requires no
database schema change. Older v0.2 databases retain all observation rows and
claim IDs through the existing v0.3 migration path.

For this Station installation (PowerShell):

```powershell
Set-Location C:\Station\SHADE
$shadePython = Join-Path $env:LOCALAPPDATA 'Python\pythoncore-3.14-64\python.exe'
& $shadePython -m venv .venv
& .\.venv\Scripts\python.exe -m pip install --no-deps --force-reinstall .\release\shade_node-0.4.0-py3-none-any.whl
& .\.venv\Scripts\shade.exe --config .\config.toml migrate
& .\.venv\Scripts\shade.exe --config .\config.toml doctor
& .\.venv\Scripts\shade.exe --config .\config.toml --mode standard status
& .\.venv\Scripts\shade.exe --config .\config.toml inbox
& .\.venv\Scripts\shade.exe --config .\config.toml now
```

If .venv already exists, omit the venv creation command. The implementation
handoff already supplies an installed local v0.4 environment; the install command
is a repeatable repair/upgrade path. Use its full executable path to avoid the
older user-level shade.exe on PATH. No change to the global Python installation
is required. Do not copy the example over config.toml or run init during upgrade.

A timestamped SQLite backup is made beside the database before legacy migration,
and the explicit migrate command makes a backup before status housekeeping.
Verify the reported backup exists. Migration is idempotent and does not remove
raw evidence. A legacy open-source grouping is preserved rather than destructively
split. Scores are re-evaluated from observations on every inbox/show invocation.

For another Windows machine use its Python 3.11+ path. Install into a fresh venv,
then point `--config` to the preserved private station file. Relative database
paths resolve against that file, not the current shell directory.

## Optional source expansion

Existing config.toml is unchanged. Merge the contents of config.local.example.toml
into a private config.local.toml beside it (do not overwrite an existing one).
Enable the desired packs only once across the two files. The local file can add
sources, relevance settings, source packs, and collection.user_agent. It cannot
silently replace your identity or database path.

## Rollback

Keep the prior wheel and the pre-upgrade backup. Do not run an older version against
an actively written v0.4 database. If rollback is needed, stop collection, preserve
the current v0.4 database separately, install the prior version into another venv, and point a separate
private configuration at a COPY of the pre-upgrade backup. Post-upgrade evidence
will remain in the v0.4 database for later reconciliation; do not discard it.
