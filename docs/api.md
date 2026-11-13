# API

[Setup](setup.md) · [Configuration](configuration.md) · [Media tools](tools.md) · [Clients](clients.md) · [API](api.md) · [The app](app.md) · [Operations](operations.md)

What the scheduler and the media server answer to over HTTP. Everything wants `Authorization: Bearer <key>`: an access token, or for what is marked "own key only" the scheduler's own key.

## Scheduler (port 8080)

| Request | What it does |
|---|---|
| `/v1/...` | Passed to the model named in the request body's `"model"`. `GET /v1/models` lists them. |
| `GET /scheduler/status` | Everything the app shows, as JSON: resources, queue, recent events, machine load, endpoints. |
| `POST /scheduler/acquire {"resource", "ttl_s"}` | Take a lease on a service; answers with its address once it is ready. `renew` and `release` take `{"lease"}`. |
| `POST /scheduler/unload {"resource"}` | Unload something idle. |
| `GET /scheduler/config` | The config file. Own key only. |
| `POST /scheduler/config {"patch": {...}, "env": {...}}` | Merge the patch into the file (null removes a key), check, save, apply; `env` sets or (null) removes entries of `.env`. `GET` returns both. Own key only. |
| `POST /scheduler/reload` | Read the config file again now. Own key only. |
| `GET /scheduler/tokens`, `POST /scheduler/mint`, `POST /scheduler/revoke` | Access tokens. Own key only. |
| `POST /scheduler/announce {"name", "url"}` | Another service says where clients find it; listed under `endpoints` in the status for 3 minutes, so it is repeated. Own key only. |

Nothing behind the scheduler can be reached without a key, and whatever changes something takes POST.

Each server's output goes to `~/.pinecone/logs/<name>.log`.

## Media server (port 8090)

| Request | What it does |
|---|---|
| `/mcp` | The MCP endpoint (streamable HTTP, stateless). Tools: the entries under `"tools"`, plus `upload_link`, `put_file`, `get_file` and `media_status`. Resources: `pinecone://files/<name>`. |
| `GET /health` | No key needed. The tools the server made, and any it left out with the reason. |
| `GET /files/<name>` | A result or uploaded file. With a key, or with the signature a tool answer's `url` carries. |
| `POST /upload?name=x.jpg` | Takes a file (raw body or a multipart `file` field). Needs a key. |
| `PUT /upload/<name>?exp=…&sig=…` | Where an upload link points. No key; the link is good for an hour. |
