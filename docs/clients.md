# Clients

[Setup](setup.md) · [Configuration](configuration.md) · [Media tools](tools.md) · [Clients](clients.md) · [API](api.md) · [The app](app.md) · [Operations](operations.md)

How a program on this Mac or another device uses the models and the tools.

## The two endpoints

Pinecone gives clients two addresses, both shown in the menu bar app with a button to copy each:

| Endpoint | What it is | Default |
|---|---|---|
| Models | An OpenAI-compatible API. The `model` in a request names an entry of the config; Pinecone loads it if needed. | `http://<this Mac>:8080/v1` |
| Media tools | An MCP server (streamable HTTP) with one tool for each entry under `"tools"`. | `http://127.0.0.1:8090/mcp` |

Both want `Authorization: Bearer <access token>`.

## Keys and tokens

Every request needs `Authorization: Bearer <key>`.

- **Access tokens** are for clients: other devices, apps, coding agents. Make and revoke them in the app's
  Settings, or with `POST /scheduler/mint {"name": "..."}` and `POST /scheduler/revoke {"id": "..."}`.
  A token can use the models, the leases and the media server. It cannot change settings or tokens.
  Only hashes of the tokens are stored (`tokens.json`), so a token is shown once, when it is made.
- **The scheduler's own key** (`internal-key.txt`) can do everything. It is for the app and for
  tools on the same Mac. `/health` tells callers on the same Mac where that file is, which is how the app
  finds it without being told.

The model servers themselves listen on `127.0.0.1` only and have no key; clients reach them through the scheduler.

## Connecting a client

Anything that speaks the OpenAI API works: give it the Models address shown under Endpoints in the app as
the base URL, an access token as the API key, and a model name from your config.

    curl http://YOUR-MAC:8080/v1/chat/completions -H "Authorization: Bearer $PINECONE_TOKEN" \
      -H "Content-Type: application/json" -d '{"model": "small", "messages": [{"role": "user", "content": "Hello"}]}'

`examples/pine-models.json` is a provider entry for [pine](https://github.com/pmndrs/pine): put it into
pine's `models.json`, set the address, and put the token into pine's `.env` as `PINECONE_TOKEN`.
For the media tools, add the Media address as an MCP server with the header `Authorization: Bearer <token>`.

## Files, from a client's point of view

A client on another computer shares no disk with the server. A tool that makes a file answers with a link; a tool that takes a file wants it sent first. [Media tools](tools.md#files) has the details. In short:

- **A result:** download the `url` from the answer. It needs no key and works for a week. Do not put the link itself into a chat reply as a picture or sound; save the file, then use the saved copy.
- **A file of your own:** call `upload_link` and send the file to the link it gives (`curl -T`), or pass its bytes to `put_file`; then give the tool the name you got back.
