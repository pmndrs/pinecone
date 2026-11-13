# Operations

[Setup](setup.md) · [Configuration](configuration.md) · [Media tools](tools.md) · [Clients](clients.md) · [API](api.md) · [The app](app.md) · [Operations](operations.md)

Running it day to day: where to look, what to run after a change, what can go wrong.

## Logs

Everything is in `~/.pinecone/logs/`: `scheduler.log`, `media.log`, and one file for each model or service, named after it. A model that fails to start says why in its own log; the app shows the last line of the reason on its row.

## Starting and stopping

`launchd/install.sh` installs two background services, `dev.pinecone.scheduler` and `dev.pinecone.media-mcp`, which start at login and are restarted if they stop. The switch at the top of the app starts and stops both. By hand:

    launchctl kickstart -k gui/$UID/dev.pinecone.scheduler      # restart; stops every loaded model
    launchd/install.sh remove                                    # stop and remove both

A change to the config needs no restart. A restart is for new program code, or a new `listen` address.

## When something is wrong

| What you see | Likely cause |
|---|---|
| A request waits and never starts | Something else is working and cannot be stopped. The app's "Waiting" section says what it waits for. |
| "port … is already in use" | A server from an earlier run is still there (the scheduler was killed). Stop that process, or restart the Mac. |
| "did not become ready in time" | The model takes longer to load than `start_timeout_s`, or its `health` path is wrong. Its log says more. |
| A model is very slow | It does not fit and macOS is swapping. Raise its `memory_gb` so less is loaded beside it, or keep more for the system. |
| "The GPU memory limit could not be set" | The helper is not installed: press "Allow…" in Settings. |
| A tool is missing from the media server | `curl localhost:8090/health` names it under `tool_errors` with the reason. |
| An agent cannot play a result | It put the link into its reply without saving the file first. The answer tells it to download; a fresh conversation helps. |

## Security

- Model servers listen on `127.0.0.1` and have no key. Clients reach them only through the scheduler, which wants a key for every path.
- Access tokens are stored as hashes. A token can use models, services and tools; it cannot change settings or tokens.
- File links are signed and end after a week; upload links after an hour. Whoever has such a link can use it, so treat one like the file itself.
- A caller that arrives through a proxy cannot make a tool read a path on the server or fetch an address that is not public.
- Expose the media server through a proxy (`tailscale serve`, a reverse proxy with TLS), not by listening on `0.0.0.0`.

## Tests

    .venv/bin/python scheduler/scheduler.py scheduler/tests/config.json &     # stand-in servers, port 18080
    .venv/bin/python scheduler/tests/run.py
    .venv/bin/python scheduler/tests/reload.py       # these two start their own scheduler
    .venv/bin/python scheduler/tests/settings.py
    .venv/bin/python media_mcp/tests/tools.py        # the tools of the example config, no services needed
