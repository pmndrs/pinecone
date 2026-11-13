# Setup

[Setup](setup.md) · [Configuration](configuration.md) · [Media tools](tools.md) · [Clients](clients.md) · [API](api.md) · [The app](app.md) · [Operations](operations.md)

From nothing to a running scheduler, the media server, the menu bar app and a start at login.

## The scheduler

    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    .venv/bin/python scheduler/scheduler.py

The first start makes `~/.pinecone/` with a `config.json` and a `.env`, copied from the examples that come
with the program. Put your paths into `.env` and your models into `config.json`; the scheduler takes the
changes up as you save. Then ask for a model by the name you gave it:

    curl http://127.0.0.1:8080/v1/chat/completions \
      -H "Authorization: Bearer $(cat ~/.pinecone/internal-key.txt)" -H "Content-Type: application/json" \
      -d '{"model": "small", "messages": [{"role": "user", "content": "Hello"}]}'

To keep it running in the background and start it at login: `launchd/install.sh` (`launchd/install.sh remove` undoes it).

## The media server

Optional. It needs ComfyUI and mlx-audio (or whatever your tools use) installed, and the two services `comfy` and `audio` in the config; the config a first start gives you has both.

    .venv/bin/python media_mcp/server.py

`launchd/install.sh` starts it together with the scheduler. `curl localhost:8090/health` lists the tools it made.

## The menu bar app

    menubar/build.sh                                   # needs only the Xcode Command Line Tools
    cp -R menubar/build/Pinecone.app /Applications/

Open it, and in Settings switch on "Open at login". See [The app](app.md).

## The GPU memory limit

`system_reserve_gb` is the one number to choose: how much the Mac keeps for itself. On Apple silicon macOS
also limits how much memory the GPU may hold, and that limit is usually too low for large models. The
scheduler raises it to "all but the reserve" when it starts and puts the macOS default back when it stops.

Changing that limit needs administrator rights once. The app asks for it (Settings, "Allow…"); without the
app, run `sudo scheduler/install-gpu-limit.sh` (`... remove` undoes it). It installs a small helper that
can do nothing but set this one limit.

`memory_budget_gb` can be given in place of `system_reserve_gb` to plan with a fixed amount; the GPU limit
is then left alone. The tests use it.

## Where things are

Everything that is yours is in `~/.pinecone/` (set `PINECONE_HOME` to use another folder). The program's own
folder holds none of it, so the program can be replaced or updated without touching your setup.

| In `~/.pinecone/` | What |
|---|---|
| `config.json` | Your models and services, the media server's settings, and the tools it offers. The one file to edit. |
| `.env` | Paths and addresses of this Mac, used in the config as `$NAME`. |
| `internal-key.txt`, `tokens.json` | The scheduler's own key, and the access tokens you made (as hashes). |
| `logs/` | Output of the scheduler, the media server and every model server. |
| `files/` | What the media tools made, and what was uploaded to them. |

An agent (Claude, Codex) that should add a model or a tool needs only `config.json` and this page.
