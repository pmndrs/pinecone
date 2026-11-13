# Working on Pinecone

Read `README.md` and then `docs/`: `configuration.md` has every config setting, `tools.md` the tool format,
`api.md` the HTTP API, `operations.md` what to run and where to look.
This file is what you need beyond that to change the code or to operate a running install.

## Layout

| Path | What |
|---|---|
| `scheduler/scheduler.py` | The whole scheduler, one file (aiohttp). |
| `scheduler/install-gpu-limit.sh` | Installs the root helper that sets the GPU memory limit. |
| `scheduler/tests/` | Tests against stand-in servers (`dummy.py`). No models needed. |
| `media_mcp/server.py` | The media MCP server, one file. It knows no media tool: it builds them from `"tools"` in the config. |
| `config.example.json`, `.env.example` | What a new user's `~/.pinecone/` starts with, including the tools that ship. |
| `menubar/Sources/Pinecone/` | The menu bar app: `App.swift` (views), `Monitor.swift` (state, requests). |
| `envfile.py` | Shared by both services: where the user's folder is, the first-start copy, `$NAME` variables, writing config. |
| `launchd/` | Templates and `install.sh` for running the services at login. |

Nothing of a user's is in this folder. Config, `.env`, key, tokens, logs and media files are in `~/.pinecone/`
(`PINECONE_HOME`). Never print `internal-key.txt`, `tokens.json` or `.env`, and never copy them into the repository.

## Operating a running install

- **Add or change a media tool:** edit `"tools"` in `~/.pinecone/config.json` and save. `curl localhost:8090/health`
  lists the tools and names any that was left out, with the reason. A new tool that should ship for everyone
  goes into `config.example.json` as well. Do not add tools to `server.py`.
- **Add or change a model:** edit `"resources"` in `~/.pinecone/config.json` and save. The scheduler reloads it by itself.
  Check the result with `GET /scheduler/status`: `config.error` is null when the file was accepted.
- **Do not restart the scheduler to apply config.** A restart stops every loaded model. If you must restart
  (code change), check `/scheduler/status` first and only do it when nothing is active or queued.
  With the launchd services: `launchctl kickstart -k gui/$UID/dev.pinecone.scheduler`.
- **Never talk to a model server's own port.** Go through the scheduler, or its memory planning is wrong.
- **Read keys from the files, never echo them.** `$(cat ~/.pinecone/internal-key.txt)` inside a command is fine.
- **A model's real memory use** is `actual_gb` in the status while it is loaded. Set `memory_gb` a little above it.

## How the scheduler works

- `load_config` reads the file, checks it (`check_config`), fills in defaults and variables.
- `Res` is one resource: `off`, `starting`, `ready` or `stopping`. `Scheduler.step()` is the only place
  that decides: it hands waiting requests to ready resources and plans one load at a time
  (`evictions` says what must stop first). It runs after every event and once a second.
- A request holds its resource "active" until it ends; a lease does the same for services.
- `reload` applies a new config without interrupting anything: policy settings at once, start settings
  (`LAUNCH_KEYS`) at the resource's next load, removed resources once idle (`settle`).
- Callers are `admin` (the scheduler's own key) or a token id (`caller`). Settings and tokens need admin.

## Changing the code

- Run all three test files after any scheduler change (see `docs/operations.md`, Tests). Add a check for new behaviour.
- After changing the media server or the tools in `config.example.json`, run `media_mcp/tests/tools.py`. It needs no services.
  To try a tool for real: `media_mcp/tests/call.py <tool> '<json arguments>'` (it loads the service).
- The app builds with the Command Line Tools only, so SwiftUI macros that need Xcode (`@State`, `#Preview`)
  are not available. View state lives in `Monitor` as `@Published` properties.
- Check design changes with `Pinecone --snapshot out.png [--settings]`; it renders the real panel.
- The app shows nothing it did not get from the scheduler. A new setting goes into the scheduler first
  (config, status), then into the app.
- Keep each service one file, and prefer a default over a required setting.
