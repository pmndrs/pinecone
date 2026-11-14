# Configuration

[Setup](setup.md) · [Configuration](configuration.md) · [Media tools](tools.md) · [Clients](clients.md) · [API](api.md) · [The app](app.md) · [Operations](operations.md)

Everything in `~/.pinecone/config.json` that concerns the scheduler. The media server's part of the file is in [Media tools](tools.md).

`~/.pinecone/config.json` is read when the scheduler starts and again whenever it is saved, so changes need
no restart. Nothing that is loaded gets interrupted: a changed start command applies the next time that
model loads, and a removed model is stopped once it is idle. A file with a mistake is ignored and the
mistake is shown in the app (and in `/scheduler/status`); the last good version stays in use.

| Setting | Meaning | If left out |
|---|---|---|
| `system_reserve_gb` | Memory kept free for macOS and your apps. Models and services share the rest. | required |
| `resources` | The models and services, by name. See below. | required |
| `listen` | `{"host", "port"}` the scheduler listens on. Use host `0.0.0.0` to reach it from other devices. | `127.0.0.1`, `8080` |
| `public_url` | The address other devices use for the scheduler, shown under Endpoints in the app. | this Mac's network name when `listen` is `0.0.0.0`, else the address the app itself uses |
| `starvation_after_s` | A request that has waited this long gets the next turn. | `1800` |
| `manage_gpu_limit` | `false`: do not touch the GPU memory limit (see Memory). | `true` |
| `api_key_file`, `tokens_file` | Where the scheduler's own key and the token list are kept. | next to the config |
| `media`, `tools` | The media server's settings and its tools. See The media server. | none |

Each entry in `resources` is one server the scheduler may start. Its name is what clients send as `"model"`.

| Setting | Meaning | If left out |
|---|---|---|
| `type` | `"llm"`: a language model, reached through `/v1/...`. `"service"`: anything else, used through leases. | `"llm"` |
| `title` | A label, shown in the app. | the name |
| `memory_gb` | Memory to plan for it. As much as there is, or more, means it runs alone. | required |
| `port` | The port the server listens on. | required |
| `cmd` | The command that starts the server, one argument per item. | required |
| `cwd` | Folder to start it in. | the scheduler's |
| `health` | Path that answers 200 once the server is ready. | `/health` |
| `start_timeout_s` | How long it may take to become ready. | `900` |
| `keep_alive_s` | How long it stays loaded after its last request even when something else wants the memory. | `300` |
| `min_residency_s` | How long it stays loaded at least, once started. | `60` |
| `idle_stop_s` | Stop it after this long without work, even if nothing else needs the memory. | never |
| `keep_cache` | For a llama.cpp server: write what its slots have read to disk before it is stopped, and read it back when it has started again. Without it, the first message of every conversation after a reload is read from its first word. The files are in `cache/<name>/` next to the config, one per slot, and can be several GB each. | `false` |

`cmd` is all the scheduler knows about a server. It runs the command, waits for `health` to answer on
`port`, and sends requests there. To unload, it ends the process it started (and force-quits it if it has
not gone after 30 seconds). There is no stop command to write.

## Variables

Paths and addresses that belong to your Mac go into a `.env` file, not into the config. A config value can
use them as `$NAME` or `${NAME}`; `$$` is a dollar sign. This is the same notation pine uses.

    # .env
    MODELS=/Volumes/Fast/models

    "cmd": ["$LLAMA_SERVER", "-m", "$MODELS/qwen.gguf", "--port", "$PORT"]

`.env` is next to the config file; the environment wins over it.
A name that is not set is reported as a mistake in the config. Always available: `$PORT` (the entry's own
port), `$CONFIG_DIR` (the folder of the config file) and `$PYTHON` (the Python the scheduler runs on).

## How it decides

- A model that is working is never stopped.
- An idle one is stopped when a request for something else does not fit next to it; the one unused longest goes first.
- Requests wait while their model loads. Streaming clients get a comment line every few seconds meanwhile.
- A request that has waited longer than `starvation_after_s` gets the next turn: nothing new is given to the models in its way.

## Memory

`system_reserve_gb` is the one number to choose: how much the Mac keeps for itself. On Apple silicon macOS
also limits how much memory the GPU may hold, and that limit is usually too low for large models. The
scheduler raises it to "all but the reserve" when it starts and puts the macOS default back when it stops.

Changing that limit needs administrator rights once. The app asks for it (Settings, "Allow…"); without the
app, run `sudo scheduler/install-gpu-limit.sh` (`... remove` undoes it). It installs a small helper that
can do nothing but set this one limit.

`memory_budget_gb` can be given in place of `system_reserve_gb` to plan with a fixed amount; the GPU limit
is then left alone. The tests use it.
