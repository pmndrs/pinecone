# Media tools

[Setup](setup.md) · [Configuration](configuration.md) · [Media tools](tools.md) · [Clients](clients.md) · [API](api.md) · [The app](app.md) · [Operations](operations.md)

The media server turns the entries under `"tools"` in the config into MCP tools. This page is the format, and how files get to and from a tool.

`media_mcp/server.py` is an MCP server (streamable HTTP at `/mcp`). It reads the same `config.json` as the
scheduler and makes one tool from every entry under `"tools"`, taking up changes as the file is saved. The
config that the first start gives you comes with `generate_image`, `edit_image`, `remove_background`,
`generate_music`, `generate_sound`, `speak` and `transcribe`, on top of two services named `comfy` (ComfyUI)
and `audio` (mlx-audio). Four tools are always there: `upload_link`, `put_file`, `get_file` and `media_status`.

    .venv/bin/python media_mcp/server.py

Its settings are under `"media"` in the config, all optional:

| Setting | Meaning | If left out |
|---|---|---|
| `models` | Names the tools can use as `$name`, such as which model file ComfyUI should load. | none |
| `comfy_input_dir` | ComfyUI's input folder, where pictures passed to a tool are put. | `files/` |
| `listen` | `{"host", "port"}` the server listens on. | `127.0.0.1`, `8090` |
| `public_url`, `allowed_hosts` | The address other devices use (shown in the app), and the host names the server answers to. Needed behind a proxy. | this Mac only |
| `keep_days` | Results and uploaded files older than this are deleted. `0` keeps them for good. | `7` |
| `comfy_output_dir` | ComfyUI's output folder. Its own copy of a result is removed from there once the result is saved. | `output` next to `comfy_input_dir` |

The server accepts the same access tokens as the scheduler, and tells the scheduler its address, which is
how it appears under Endpoints in the app. `GET /health` lists the tools it made and names any it had to
leave out, with the reason.

## Files

**Files travel with the calls.** Client and server are usually not the same computer, so no tool reads or
writes a path on the caller's disk. There are two ways in each direction, and a client uses whichever it can:

| | Inside the MCP call | Beside it, over HTTP |
|---|---|---|
| Getting a result | `get_file` returns any file as part of its answer, and `resources/read` returns `pinecone://files/<name>`. With `inline_mb` set, a sound file also comes inside the answer that made it. | The answer's `url`. It is built on the address the caller used and carries its own permission (a signature and an end date, a week ahead), so a download tool, a player or `curl` fetches it without a key. |
| Giving a file | `put_file` takes the file's bytes (base64). File parameters are marked `"x-file": true` in the tool's schema, for clients that send files by themselves. | `upload_link`, and the error for a path the server cannot see, answer with a link to `PUT` the file to (no key, one hour) and the name to pass afterwards. An http(s) link to the file works as an argument too. |

Every result is kept in `~/.pinecone/files/` for `keep_days` (a week), then deleted; a link ends when its file does. Each answer starts by telling the calling model that it does not
have the file yet and has to download it. Sound is not sent inside answers unless `inline_mb` says so: a client
that does not keep such content shows its model a placeholder, and the model then takes the file for delivered.

A full path on the server, and links to addresses that are not public, only work for callers on the same
Mac: a call that arrives through a proxy (`tailscale serve`, a reverse proxy) is treated as coming from
another machine. So expose the server through a proxy, not by listening on `0.0.0.0`.

## A tool

    "tools": {
      "generate_sound": {
        "description": "Create a sound effect.",
        "service": "comfy",
        "params": {
          "prompt": {"type": "string", "required": true, "description": "Describe the sound."},
          "duration_seconds": {"type": "number", "default": 5, "min": 1, "max": 190},
          "kind": {"type": "string", "enum": ["sound_effect", "ambience_or_loop"], "default": "sound_effect"},
          "seed": {"type": "integer", "default": "random"}
        },
        "values": {"checkpoint": {"from": "$kind", "map": {"sound_effect": "$sfx_checkpoint", "ambience_or_loop": "$audio_checkpoint"}}},
        "run": {"comfy": { ...the workflow, with "$prompt", "$seed" and so on where the values go... }},
        "result": {"save": "sound", "report": {"duration_seconds": "$duration_seconds"}}
      }
    }

The name of the entry is the name of the tool. The server does the rest for every tool: it asks the
scheduler for the service, reports the wait, saves the result and answers with the file's name and link.

**`params`** are what the caller may pass. Each has a `type` and optionally `description`, `required`,
`default` and `enum`.

| Type | What the tool gets |
|---|---|
| `string`, `integer`, `number`, `boolean` | The value. Numbers are kept within `min` and `max`. `"default": "random"` makes a fresh number for each call. A string with `"lower": true` is put in lower case; one that does not fit `"match"` (a pattern) counts as not given. |
| `image`, `images` | The picture is fetched, turned upright and put where ComfyUI can load it; the value is its name there (a list of names for `images`, at most `max`). |
| `file` | The file is fetched, to be sent on with `upload`. |

**`values`** are worked out from the parameters, in order, and can be used like them. A value is a template,
or one of:

| Form | Result |
|---|---|
| `{"from": "$size", "map": {"standard": 1024, "large": 2048}}` | Looks a value up. `"else"` is used when it is not in the map (without it, that is an error); `"prefix": "-"` also tries `pt` for `pt-br`; `"times": "$n"` multiplies the result. |
| `{"first": ["$voice", "Ryan"]}` | The first one that is not empty. |
| `{"pick": ["C major", "A minor"]}` | One at random; the same one again for the same `seed`. |
| `{"list": ["$image", "$reference_images"]}` | Several values and lists as one list. |

**Templates.** Anywhere in `values`, `run` and `result`, `$name` stands for a parameter, a value, or an entry
of `models` in the config. A string that is exactly one `$name` becomes that value as it is, so a number
stays a number; inside longer text it becomes text. `$$` is a dollar sign. What a caller passed is never
read as a template. A name that does not exist is found when the config is saved, not when the tool is called.

**`run`** is one of:

- `{"comfy": {...}}`: a ComfyUI workflow in API format (in ComfyUI: Export (API)), with `$names` where the
  values go. It can also be the name of a file next to the config that holds the workflow. `"repeat": {"over": "$images", "patch": {...}}` adds a piece of workflow once for each
  item of a list, with `$i` (counting from 1) and `$item`.
- `{"post": "/v1/audio/speech", "json": {...}}`: one request to the service. `"form"` sends form fields
  instead, and `"upload": {"file": "$audio"}` attaches a `file` parameter. An entry that comes out empty is left out.
- `{"python": "my_tools.py:function"}`: a function in a file next to the config, for what the above cannot
  describe. It is called with the tool's values and a helper (`scheduler(path)`, `lease(service)`,
  `upload_link(filename)`, `headers`, `error(message)`) and returns text or something JSON can hold.

**`variants`** are for a tool that works in more than one way. The first one whose `when` holds is used; it
can replace `run` and `result`, add or replace `values`, and `patch` the workflow. In `when`, each entry
names a parameter and what it must be: a value, `{"in": [...]}`, or `{"given": true}`.

    "variants": [{"when": {"fast": true}, "patch": {"sample": {"inputs": {"steps": 8}}}}]

**`result`** says what comes back. `{"save": "sound"}` saves the file the service made (name starts with
`sound`; give `"ext"` when the service does not name its file) and answers with its name and link;
`"preview": true` adds a small picture for images; `"report"` adds entries to the answer.
`{"fields": ["text", "language"]}` is for a service that answers with JSON: those fields are passed on.

To add a tool: add an entry under `"tools"` and save. `media_mcp/tests/tools.py` checks every tool of the
example config without any service running.
