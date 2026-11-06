# Aid_Map

## Database CLI

The CLI stores data in a persistent database at `%LOCALAPPDATA%\Aid_Map\aid_map.kv`
on Windows (`~/.local/share/Aid_Map/aid_map.kv` by default on other platforms).
The same database is used when the path is omitted, so data remains available
between commands.

Run commands from the repository root:

```powershell
python -m src.cli put name Anu
python -m src.cli get name
python -m src.cli del name
```

Use `--db PATH` to select another database. Existing path-first commands are also
supported, for example `python -m src.cli put custom.kv name Anu`.
