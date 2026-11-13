# GuideBot

Telegram bot for restaurant search in Bishkek and Almaty using 2GIS reviews,
trust scores, semantic search, and location filters.

## Existing systemd installation

The systemd unit and `setup_systemd.sh` are still available. On that installation,
use `./update_city.sh CITY` for data maintenance. The host script manages the
systemd service. The Compose commands below manage only Compose containers.
Choose one deployment for a given token and data directory.

## Run with Docker

Requires a Linux host with Docker Engine, Docker Compose v2, Bash, Python 3,
and `flock`. Python dependencies are installed from `uv.lock` during the build;
containers do not install packages on startup.

```bash
cp .env.example .env
# Fill in credentials and allowed Telegram usernames.
./manage.sh init    # May require root to set directory ownership.
./manage.sh build
# Restore data/ or collect it before starting the bot.
./manage.sh up
./manage.sh status
./manage.sh logs
```

Only one instance may poll a Telegram token. Stop an existing systemd or manual
instance before `up`. Compose cannot stop processes managed outside Compose.
Validate Telegram and LLM API access from the destination before moving production.

`bot` runs continuously; `collector` runs only for maintenance. The bot image has
no browser. Both run as a non-root user, with dropped capabilities, resource limits,
and rotated Docker logs. No inbound ports are required. Health checks verify recent
successful Telegram polling, not LLM availability or answer quality.

```bash
./manage.sh update bishkek --test
./manage.sh update almaty --from trust
./manage.sh backup
./manage.sh restore /absolute/path/to/data-TIMESTAMP.tar
```

Update, backup, and restore stop a running Compose bot and restart it on exit,
including on failure. An already stopped bot remains stopped. Updates are locked
against concurrent runs. See [data collection](docs/update-city-data.md).

## Code and deployment data

Commit source, `pyproject.toml`, `uv.lock`, Docker/Compose configuration, scripts,
tests, these instructions, and the empty `.env.example` template.
Do not commit credentials, IP addresses, host-specific paths, logs, databases,
user state, or local assistant context.

| Host path (default) | Contents |
|---|---|
| `data/` | City SQLite databases, Chroma indexes, `bot_persistence.pickle` |
| `cache/` | Downloaded embedding model and browser driver cache |
| `logs/` | Application logs; separate from Docker's rotated stdout logs |
| `backups/` | Data archives and SHA-256 checksums |
| `.env` | Credentials and optional host-specific directory/UID settings |
| `CLAUDE.local.md`, `.claude/` | Optional local-only deployment context |

All these paths are excluded from Git and Docker build inputs. Preserve **all of
`data/`**, including the conversation state. A Git clone is not a data backup.
Treat user data and logs as private. Copy backups off the server and manage retention;
this project does not automatically delete archives or application log files.
Run backup and restore with permission to manage files owned by the configured container UID.
Restore validates checksums, archive paths, and SQLite integrity, and retains the
previous data directory. Integrity checks on large databases can take several minutes.
Restore only trusted backups: bot state uses Python pickle.
Set `BACKUP_DIR=/absolute/path ./manage.sh backup` to change the archive directory.

Cities and paths are in `config.py`. The LLM model is in `agent/core.py`, the prompt
in `agent/prompt.txt`. Changing the application requires rebuilding its image.
The collector supports other 2GIS search terms, but the bot and schema remain
restaurant-specific; Docker does not make them a generic search application.

## Development and tests

```bash
uv sync --locked
uv run python -m unittest discover -s tests -v
```

Tests run on Linux with `flock`. They use temporary databases and mocked network
calls, do not message users, and do not access production data. Keep real scraping,
LLM, and backup/restore smoke tests separate from the offline test suite.
