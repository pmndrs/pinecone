#!/usr/bin/env python3
"""Pinecone media server (MCP).

Serves the tools described under "tools" in the config file (make an image, edit a photo, compose
music, speak ...): each names a service from the same file and says what to send it. Before
touching a service, a tool asks the scheduler for a lease, so the scheduler can bring the service
into memory, or make the call wait while something else needs the machine.

Every request needs "Authorization: Bearer <key>": the scheduler's own key, or an access token made in the
menu bar app (a revoked token can keep working here for up to a minute).
"""
import asyncio
import base64
import contextlib
import copy
import hashlib
import hmac
import importlib.util
import inspect
import io
import ipaddress
import json
import mimetypes
import random
import re
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import parse_qs, quote, urlsplit

import anyio
import httpx
import uvicorn
from pydantic import Field
from PIL import Image as PILImage, ImageOps
from starlette.responses import FileResponse, JSONResponse

from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import AudioContent, BlobResourceContents, EmbeddedResource

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from envfile import config_path, env_mtime, expand, read_env  # noqa: E402

CONFIG = config_path(sys.argv)  # the same file the scheduler reads; this server uses "media" and "tools" in it


def read_config():
    """The whole file, and this server's part of it with the variables put in and defaults for what is left out."""
    whole, env = json.loads(CONFIG.read_text()), read_env(CONFIG)
    listen = {"host": "127.0.0.1", "port": 8090, **whole.get("media", {}).get("listen", {})}
    mine = expand({"files_dir": "$CONFIG_DIR/files", "models": {},
                   "scheduler_url": f"http://127.0.0.1:{whole.get('listen', {}).get('port', 8080)}",
                   "api_key_file": whole.get("api_key_file", "$CONFIG_DIR/internal-key.txt"), **whole.get("media", {}),
                   "listen": listen}, env)
    mine.setdefault("public_url", f"http://{mine['listen']['host']}:{mine['listen']['port']}")
    mine.setdefault("allowed_hosts", [f"127.0.0.1:{mine['listen']['port']}", f"localhost:{mine['listen']['port']}"])
    return whole, mine


_, CFG = read_config()
for _ in range(30):  # the scheduler makes the key on its first start; give it a moment
    if Path(CFG["api_key_file"]).exists():
        break
    time.sleep(1)
KEY = Path(CFG["api_key_file"]).read_text().strip()
FILES = Path(CFG["files_dir"])
FILES.mkdir(parents=True, exist_ok=True)
COMFY_IN = Path(CFG.get("comfy_input_dir", FILES))  # where ComfyUI looks for input images
# Where ComfyUI keeps its own copy of what it makes, which is removed once the result is saved here.
COMFY_OUT = Path(CFG["comfy_output_dir"]) if "comfy_output_dir" in CFG else COMFY_IN.parent / "output" if "comfy_input_dir" in CFG else None
KEEP_DAYS = float(CFG.get("keep_days", 7))  # results and uploads older than this are deleted; 0: kept for good
M = CFG["models"]  # names a tool can use as $name, e.g. which model file to load; read again with the tools
SCHED = CFG["scheduler_url"]
AUTH = {"Authorization": f"Bearer {KEY}"}

mcp = MCPServer(
    "pinecone-media",
    instructions=(
        "Creates and edits media on a server that is usually not the computer you run on, so never look for "
        "its files on your disk. "
        "GETTING A RESULT: a tool saves its result on the server and answers with its name (`file`) and a link "
        "(`url`). That answer is not the file: you do not have it until you save it with your download tool "
        "(the link needs no key). Never put the link itself into a reply as a picture or sound; it does not load "
        "there. Save the file first, then play, show or attach your saved copy. To work on it with another tool "
        "here, pass `file`. "
        "GIVING A FILE: a tool cannot read a path on your computer. Send the file first: with put_file if you can "
        "pass its bytes, else with upload_link, which gives you a link to send it to. Then pass the `file` name "
        "you got. An http(s) link to the file works too. "
        "Calls can take minutes: the server may first have to move a large model out of memory, and the call "
        "waits in a queue for that."
    ),
)


# ---- helpers -----------------------------------------------------------
NEXT = ("This answer is not the file, and you do not have it yet. Do not put `url` into your reply as a link, picture "
        "or sound: it will not load there. Step 1: save the file with your download tool (url: the `url` below, "
        "path: the `file` name). Step 2: play, show or attach the copy you saved. Only to use it in another tool of "
        "this server, pass `file` and skip the download.")
INLINE = int(float(CFG.get("inline_mb", 0)) * 2 ** 20)  # sound up to this size also travels inside the answer; 0: never
LINK_DAYS, UPLOAD_MINUTES = min(7, KEEP_DAYS) or 7, 60  # a link does not outlive its file

# Links to files carry their own permission, so that a download tool, a player or curl can use them without the
# key: the path, the time it runs out, and a signature only this server can make.
SIGNING_KEY = hashlib.sha256(b"pinecone file links:" + KEY.encode()).digest()


def signed(path, seconds):
    exp = int(time.time()) + seconds
    sig = hmac.new(SIGNING_KEY, f"{path}:{exp}".encode(), "sha256").hexdigest()[:40]
    return f"{path}?exp={exp}&sig={sig}"


def signature_ok(path, query):
    q = parse_qs(query)
    exp, sig = q.get("exp", [""])[0], q.get("sig", [""])[0]
    if not exp.isdigit() or int(exp) < time.time():
        return False
    return hmac.compare_digest(sig, hmac.new(SIGNING_KEY, f"{path}:{exp}".encode(), "sha256").hexdigest()[:40])


def address(headers):
    """The address the caller reached this server at, so the links in an answer work from where the caller is."""
    h = {k.lower(): v for k, v in (headers or {}).items()}
    host = h.get("x-forwarded-host") or h.get("host")
    return f"{h.get('x-forwarded-proto', 'http')}://{host}" if host else CFG["public_url"].rstrip("/")


def upload_link(filename, headers):
    """A place to send one file to, and the command that does it."""
    ext = Path(filename or "").suffix.lstrip(".").lower() or "bin"
    name = new_name("upload", ext if ext.isalnum() and len(ext) <= 5 else "bin")
    url = address(headers) + signed(f"/upload/{name}", UPLOAD_MINUTES * 60)
    return {"file": name, "upload_url": url, "command": f"curl -sS -T '{filename or '<path to your file>'}' '{url}'",
            "then": f"Run the command on the computer that has the file (the link needs no key and works for "
                    f"{UPLOAD_MINUTES} minutes), then pass \"{name}\" wherever a tool of this server takes a file."}


def kind_of(name):
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def in_band(name, data):
    """Sound as part of the answer itself, for a client that plays or saves what arrives that way. Off unless
    "inline_mb" is set: a client that does not keep such parts shows the model a placeholder where the sound was,
    and the model then takes the file for delivered."""
    if INLINE and kind_of(name).startswith("audio/") and len(data) <= INLINE:
        return [AudioContent(data=base64.b64encode(data).decode(), mimeType=kind_of(name))]
    return []


def not_here(ref, ctx):
    """What to tell a caller who named a file this server cannot see: how to send it."""
    up = upload_link(ref, getattr(ctx, "headers", None))
    return ToolError(f"{ref!r} is not on this server; if it is a file on your computer, the server cannot read it there. "
                     f"Send it first by running: {up['command']}  Then call this tool again with \"{up['file']}\" in its place.")

def new_name(kind, ext):
    return f"{kind}-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(4)}.{ext}"


OURS = re.compile(r".+-\d{8}-\d{6}-[0-9a-f]{8}\.\w+$")  # what new_name makes: nothing else in the folder is touched


def sweep(now=None):
    """Delete results and uploads older than keep_days. Returns how many went."""
    days = KEEP_DAYS
    with contextlib.suppress(OSError, ValueError, TypeError):  # the setting as it is now, without a restart
        days = float(read_config()[1].get("keep_days", 7))
    if days <= 0:
        return 0
    gone, before = 0, (now or time.time()) - days * 86400
    for f in FILES.iterdir():
        with contextlib.suppress(OSError):
            if f.is_file() and OURS.fullmatch(f.name) and f.stat().st_mtime < before:
                f.unlink()
                gone += 1
    return gone


def drop_comfy_copy(item):
    """ComfyUI keeps what it made in its output folder; the result is saved here, so that copy can go."""
    if COMFY_OUT is None or item.get("type", "output") != "output":
        return
    with contextlib.suppress(OSError, ValueError):
        f = (COMFY_OUT / item.get("subfolder", "") / item["filename"]).resolve()
        f.relative_to(COMFY_OUT.resolve())  # never outside ComfyUI's folder
        f.unlink()


def describe(name, headers=None, **extra):
    p = FILES / name
    # No path: a caller on another computer takes it for a path on its own disk. `where` says in
    # every answer what a model otherwise guesses: the result is here, not with the caller.
    return {"next": NEXT, "file": name, "url": address(headers) + signed(f"/files/{name}", LINK_DAYS * 86400),
            "size_kb": round(p.stat().st_size / 1024), **extra}


async def progress(ctx, message):
    if ctx is None:
        return
    with contextlib.suppress(Exception):
        await ctx.report_progress(0, None, message)


async def even_if_cancelled(send):
    """Make one short request to the end although the call around it was cancelled (its client went away).
    Without this a cancelled call cannot say goodbye: every await in it fails at once."""
    with anyio.CancelScope(shield=True), contextlib.suppress(Exception):
        with anyio.fail_after(10):
            await send()


@contextlib.asynccontextmanager
async def lease(resource, who, ctx=None):
    """Hold a scheduler lease on a service for the length of the block."""
    async with httpx.AsyncClient(timeout=None, headers=AUTH) as c:
        t = asyncio.ensure_future(c.post(f"{SCHED}/scheduler/acquire",
                                         json={"resource": resource, "ttl_s": 600, "who": who}))
        try:
            waited = 0
            while True:
                done, _ = await asyncio.wait([t], timeout=15)
                if done:
                    break
                waited += 15
                await progress(ctx, f"waiting {waited}s for memory on the server ({resource})")
            r = t.result()
        except httpx.HTTPError as e:
            raise ToolError(f"the scheduler at {SCHED} did not answer: {e}") from e
        except BaseException:
            t.cancel()
            raise
        if r.status_code != 200:
            raise ToolError(f"the scheduler did not grant {resource}: {r.text[:300]}")
        info = r.json()

        async def renew():
            while True:
                await asyncio.sleep(120)
                with contextlib.suppress(Exception):
                    await c.post(f"{SCHED}/scheduler/renew", json={"lease": info["lease"], "ttl_s": 600})

        keeper = asyncio.ensure_future(renew())
        try:
            yield info["url"]
        finally:
            keeper.cancel()
            await even_if_cancelled(lambda: c.post(f"{SCHED}/scheduler/release", json={"lease": info["lease"]}))


MAX_FETCH = 256 << 20   # the largest file taken from a link
COMFY_PATIENCE = 3600    # seconds a ComfyUI job may take before it is given up


FORWARDED = ("x-forwarded-for", "x-forwarded-host", "forwarded", "x-real-ip", "tailscale-user-login")


def from_outside(ctx):
    """True when the call came through a proxy (tailscale serve, a reverse proxy) and so from another machine.
    This server listens on this Mac only, so a call without a proxy's headers was made on this Mac. A proxy adds
    them itself; a caller on this Mac who adds them only makes itself count as outside."""
    headers = getattr(ctx, "headers", None) if ctx is not None else None
    return bool(headers) and any(h in headers for h in FORWARDED)


async def outside_may_fetch(url):
    """Callers from other machines may not use this server to reach addresses only this Mac or its network can see."""
    host = urlsplit(url).hostname or ""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    except OSError:
        return False
    return bool(infos) and all(ipaddress.ip_address(i[4][0].split("%")[0]).is_global for i in infos)


async def fetch_bytes(ref, ctx=None):
    """A file given by name, server path, URL or data: URL. Callers from other machines get names and public links only."""
    outside = from_outside(ctx)
    if ref.startswith("data:"):
        return base64.b64decode(ref.split(",", 1)[1])
    if ref.startswith(("http://", "https://")):
        u = urlsplit(ref)
        if u.path.startswith("/files/") and (FILES / Path(u.path).name).is_file():  # a link this server gave out
            ref = Path(u.path).name
        else:
            if outside and not await outside_may_fetch(ref):
                raise ToolError(f"{ref} is not a public address, and this server does not fetch private ones for callers "
                                "on other machines. If you have the file, send it with upload_link and pass its name.")
            try:
                async with httpx.AsyncClient(timeout=120, follow_redirects=not outside) as c, c.stream("GET", ref) as r:
                    r.raise_for_status()
                    data = b""
                    async for chunk in r.aiter_bytes():
                        data += chunk
                        if len(data) > MAX_FETCH:
                            raise ToolError(f"{ref} is larger than {MAX_FETCH >> 20} MB.")
                    return data
            except httpx.HTTPError as e:
                raise ToolError(f"Could not fetch {ref}: {e}. If you have the file, send it with upload_link and pass its name.") from e
    p = Path(ref).expanduser()
    mine = not p.is_absolute()
    if mine:
        p = FILES / p.name
    if (not mine and outside) or not p.is_file():
        # ToolError, like every failure a caller can do something about: any other exception reaches the model
        # as "Error executing tool ..." and nothing else, and it starts guessing. This one says what to run.
        raise not_here(ref, ctx)
    return p.read_bytes()


async def stage_image(ref, ctx=None):
    """Put an input image where ComfyUI can load it; returns its name there."""
    try:
        img = PILImage.open(io.BytesIO(await fetch_bytes(ref, ctx)))
    except OSError as e:
        raise ToolError(f"{ref!r} is not an image this server can read (png, jpg, gif and webp are fine).") from e
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGBA" if "A" in img.getbands() or img.mode == "P" else "RGB")
    name = f"mcp_{secrets.token_hex(6)}.png"
    img.save(COMFY_IN / name)
    return name


async def run_comfy(base, graph, ctx, what):
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(f"{base}/prompt", json={"prompt": graph})
        if r.status_code != 200:
            raise ToolError(f"ComfyUI refused the job: {r.text[:500]}")
        pid = r.json()["prompt_id"]
        t0 = time.time()
        try:
            while True:
                await asyncio.sleep(1)
                if time.time() - t0 > COMFY_PATIENCE:  # ComfyUI lost the job, e.g. it was restarted: do not hold it forever
                    raise ToolError(f"ComfyUI did not finish within {COMFY_PATIENCE // 60} minutes.")
                h = (await c.get(f"{base}/history/{pid}")).json().get(pid)
                if h and h.get("status", {}).get("completed"):
                    break
                if h and h.get("status", {}).get("status_str") == "error":
                    msgs = [m[1].get("exception_message", "") for m in h["status"].get("messages", []) if m[0] == "execution_error"]
                    raise ToolError("ComfyUI failed: " + (" ".join(msgs)[:500] or "it was stopped"))
                if int(time.time() - t0) % 15 == 0:
                    await progress(ctx, f"{what}: {int(time.time() - t0)}s")
        except BaseException:  # nobody waits for this job any more: it must not go on working in a service we let go of
            await even_if_cancelled(lambda: c.post(f"{base}/queue", json={"delete": [pid]}))
            await even_if_cancelled(lambda: c.post(f"{base}/interrupt", json={"prompt_id": pid}))
            raise
        out = []
        for node in h["outputs"].values():
            for items in node.values():
                if isinstance(items, list):
                    for it in items:
                        if isinstance(it, dict) and "filename" in it:
                            v = await c.get(f"{base}/view", params={"filename": it["filename"],
                                                                      "subfolder": it.get("subfolder", ""),
                                                                      "type": it.get("type", "output")})
                            v.raise_for_status()
                            out.append((it["filename"], v.content))
                            drop_comfy_copy(it)
        if not out:
            raise ToolError("ComfyUI finished but produced no file")
        return out, time.time() - t0


def preview(data):
    img = PILImage.open(io.BytesIO(data))
    info = {"width": img.width, "height": img.height, "transparent": "A" in img.getbands()}
    if "A" in img.getbands():  # show transparency as plain light grey
        bg = PILImage.new("RGB", img.size, (236, 236, 236))
        bg.paste(img, mask=img.getchannel("A"))
        img = bg
    img = img.convert("RGB")
    img.thumbnail((1024, 1024))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return info, Image(data=buf.getvalue(), format="jpeg")


# ---- tools made from the config ----------------------------------------
# Each entry under "tools" in the config file describes one tool: its parameters, how the values for the
# request are worked out, the request itself (a ComfyUI workflow, an HTTP call to a service, or a Python
# function), and what to do with the result. README.md has the format. Nothing below knows any particular tool.
VAR = re.compile(r"\$(?:\$|\{(\w+)\}|(\w+))")
FILE_NOTE = (" Give the `file` name from an earlier answer of this server, or an http(s) link. A file on your own "
             "computer has to be sent here first, with put_file or upload_link; then pass the name you got.")
PY_TYPES = {"string": str, "integer": int, "number": float, "boolean": bool, "image": str, "file": str, "images": list[str]}
OPERATORS = ("from", "first", "pick", "list")


class Upload:
    """A file a caller passed, to be sent on to a service."""
    def __init__(self, name, data):
        self.name, self.data = name, data


def fill(template, env):
    """Put values into a template. A string that is exactly one $name becomes that value as it is (a number stays a
    number, a list a list); inside longer text it becomes text. Values that were put in are never looked at again,
    so a caller's "$" stays a "$"."""
    if isinstance(template, dict):
        return {fill(k, env): fill(v, env) for k, v in template.items()}
    if isinstance(template, list):
        return [fill(v, env) for v in template]
    if not isinstance(template, str):
        return template
    whole = VAR.fullmatch(template)
    if whole and whole.group(0) != "$$":
        return env[whole.group(1) or whole.group(2)]
    return VAR.sub(lambda m: "$" if m.group(0) == "$$" else str(env[m.group(1) or m.group(2)]), template)


def names_in(template):
    """Every $name a template uses."""
    if isinstance(template, dict):
        return set().union(*(names_in(k) | names_in(v) for k, v in template.items())) if template else set()
    if isinstance(template, list):
        return set().union(*(names_in(v) for v in template)) if template else set()
    if isinstance(template, str):
        return {m.group(1) or m.group(2) for m in VAR.finditer(template) if m.group(0) != "$$"}
    return set()


def merge(into, patch):
    """Put a patch into a nested dict; null removes a key."""
    for k, v in patch.items():
        if v is None:
            into.pop(k, None)
        elif isinstance(v, dict) and isinstance(into.get(k), dict):
            merge(into[k], v)
        else:
            into[k] = copy.deepcopy(v)
    return into


def empty(v):
    return v is None or v == [] or (isinstance(v, str) and not v.strip())


def word(v):
    """A value as the key of a map: true and false are written as in JSON."""
    return str(v).lower() if isinstance(v, bool) else str(v)


def value_of(spec, env):
    """Work out one entry of "values"."""
    if not isinstance(spec, dict) or not any(k in spec for k in OPERATORS):
        return fill(spec, env)
    if "from" in spec:  # look a value up in a map
        key, table = word(fill(spec["from"], env)), spec["map"]
        if key not in table and "prefix" in spec:
            key = key.split(spec["prefix"])[0]
        if key not in table and "else" not in spec:
            raise ToolError(f"{key!r} is not one of {', '.join(table)}.")
        out = fill(table[key] if key in table else spec["else"], env)
        return out * fill(spec["times"], env) if "times" in spec else out
    if "first" in spec:  # the first one that is not empty
        return next((v for v in (value_of(s, env) for s in spec["first"]) if not empty(v)), "")
    if "pick" in spec:   # one at random; the same one again for the same seed
        return random.Random(env.get("seed")).choice(spec["pick"])
    out = []             # "list": several values and lists as one list
    for s in spec["list"]:
        v = value_of(s, env)
        out += v if isinstance(v, list) else ([] if empty(v) else [v])
    return out


def applies(when, env):
    """Does a variant's "when" hold? Each entry is a value to equal, {"in": [...]} or {"given": true|false}."""
    for name, want in when.items():
        got = env.get(name)
        if isinstance(want, dict) and "in" in want:
            ok = got in want["in"]
        elif isinstance(want, dict) and "given" in want:
            ok = empty(got) != want["given"]
        else:
            ok = (empty(got) and empty(want)) or got == want
        if not ok:
            return False
    return True


def workflow(run, path):
    """A tool's ComfyUI workflow: written out in the config, or a file named there (looked for next to the config)."""
    w = run["comfy"]
    return copy.deepcopy(w) if isinstance(w, dict) else json.loads((path.parent / w).read_text())


def check_tool(spec, path):
    """Raise ValueError when a tool cannot work, so a mistake shows when the config is saved, not when the tool is called."""
    for k in ("name", "description", "run"):
        if k not in spec:
            raise ValueError(f'"{k}" is missing')
    params = spec.get("params", {})
    for n, p in params.items():
        if p.get("type") not in PY_TYPES:
            raise ValueError(f'{n}: "type" must be one of {", ".join(PY_TYPES)}')
    runs = [spec["run"]] + [v["run"] for v in spec.get("variants", []) if "run" in v]
    for run in runs:
        kind = next((k for k in ("comfy", "post", "python") if k in run), None)
        if kind is None:
            raise ValueError('"run" needs "comfy" (a workflow file), "post" (a path on the service) or "python" (file:function)')
        if kind != "python" and "service" not in spec:
            raise ValueError('"service" is missing: the name of the service in the scheduler config')
        if kind == "comfy" and isinstance(run["comfy"], str) and not (path.parent / run["comfy"]).is_file():
            raise ValueError(f'no workflow file {run["comfy"]} next to the config file')
        if kind == "python" and not (path.parent / run["python"].partition(":")[0]).is_file():
            raise ValueError(f'no file {run["python"].partition(":")[0]} next to the config file')
    known = set(params) | set(M) | {"i", "item"}
    for v in [spec] + spec.get("variants", []):
        known |= set(v.get("values", {}))
    used = names_in([spec.get("values"), spec.get("result"), [v for v in spec.get("variants", [])],
                     [{k: v for k, v in run.items() if k != "comfy"} for run in runs]])
    for run in runs:
        if "comfy" in run:
            used |= names_in(workflow(run, path))
    if used - known:
        raise ValueError("unknown name(s): " + ", ".join("$" + n for n in sorted(used - known)))


async def prepare(spec, args, ctx, staged):
    """The caller's arguments as values: defaults filled in, numbers kept within their limits, files fetched."""
    env = dict(M)
    for name, p in spec.get("params", {}).items():
        v, kind = args.get(name), p["type"]
        if isinstance(v, str) and "match" in p and not re.search(p["match"], v):
            v = None
        if empty(v):
            if p.get("required"):
                raise ToolError(f"{name} is needed.")
            v = p.get("default", [] if kind == "images" else "" if kind in ("string", "image", "file") else None)
            if v == "random":
                v = random.randrange(2 ** 48)
        elif kind in ("integer", "number"):
            v = (int if kind == "integer" else float)(max(p.get("min", v), min(p.get("max", v), v)))
        elif kind == "string" and p.get("lower"):
            v = v.lower()
        elif kind == "image":
            v = await stage_image(v, ctx)
            staged.append(v)
        elif kind == "images":
            v = [await stage_image(r, ctx) for r in v[:p.get("max", len(v))]]
            staged += v
        elif kind == "file":
            v = Upload(Path(v).name[:80] if not v.startswith("data:") else "file", await fetch_bytes(v, ctx))
        if kind == "number" and v is not None:
            v = float(v)
        env[name] = v
    return env


async def plan(spec, path, args, ctx, staged):
    """What a call comes to: the values, the request to make and what to do with the answer. Nothing is sent yet."""
    env = await prepare(spec, args, ctx, staged)
    variant = next((v for v in spec.get("variants", []) if applies(v.get("when", {}), env)), {})
    for name, s in {**spec.get("values", {}), **variant.get("values", {})}.items():
        env[name] = value_of(s, env)
    run = variant.get("run", spec["run"])
    out = {"env": env, "result": variant.get("result", spec.get("result", {})), "service": spec.get("service")}
    if "comfy" in run:
        graph = workflow(run, path)
        merge(graph, variant.get("patch", {}))
        graph = fill(graph, env)
        if "repeat" in run:  # the same piece of workflow once for each item of a list, numbered from 1
            for i, item in enumerate(fill(run["repeat"]["over"], env), 1):
                merge(graph, fill(run["repeat"]["patch"], {**env, "i": i, "item": item}))
        out["comfy"] = graph
    elif "post" in run:
        out["post"] = run["post"]
        for part in ("json", "form"):  # an entry that comes out empty is left out of the request
            if part in run:
                out[part] = {k: v for k, v in fill(run[part], env).items() if not empty(v)}
        out["upload"] = fill(run.get("upload", {}), env)
    else:
        out["python"] = run["python"]
    return out


class Api:
    """What a tool written in Python gets to work with: "run": {"python": "my_tools.py:function"} names a function
    in a file next to the config; it is called with the tool's values and one of these, and returns text or JSON."""
    lease = staticmethod(lease)

    def __init__(self, ctx):
        self.headers = dict(getattr(ctx, "headers", None) or {})  # of the call, e.g. to know the caller's address

    @staticmethod
    def error(message):
        return ToolError(message)

    def upload_link(self, filename):
        return upload_link(filename, self.headers)

    @staticmethod
    async def scheduler(path):
        try:
            async with httpx.AsyncClient(timeout=10, headers=AUTH) as c:
                return (await c.get(SCHED + path)).json()
        except (httpx.HTTPError, ValueError) as e:
            raise ToolError(f"the scheduler at {SCHED} did not answer: {e}") from e


def python_function(path, ref):
    file, _, name = ref.partition(":")
    mod = importlib.util.module_from_spec(s := importlib.util.spec_from_file_location(f"tool_{Path(file).stem}", path.parent / file))
    s.loader.exec_module(mod)
    return getattr(mod, name)


async def execute(spec, path, args, ctx):
    """Run one call of a tool from start to answer."""
    name, staged, t0 = spec["name"], [], time.time()
    try:
        p = await plan(spec, path, args, ctx, staged)
        env, result = p["env"], p["result"]
        if "python" in p:
            out = await python_function(path, p["python"])(env, Api(ctx))
            return out if isinstance(out, str) else json.dumps(out)
        async with lease(p["service"], name, ctx) as base:
            if "comfy" in p:
                files, _ = await run_comfy(base, p["comfy"], ctx, name)
                got_name, data, text = files[0][0], files[0][1], None
            else:
                async with httpx.AsyncClient(timeout=None) as c:
                    r = await c.post(base + p["post"], json=p.get("json"), data=p.get("form"),
                                     files={k: (u.name, u.data) for k, u in p["upload"].items()} or None)
                if r.status_code != 200:
                    raise ToolError(f"{name} failed: {r.text[:500]}")
                got_name, data, text = "", r.content, r.text
    finally:
        for s in staged:
            with contextlib.suppress(OSError):
                (COMFY_IN / s).unlink()
    if "fields" in result:  # the answer is JSON (or several lines of it): hand on the named fields
        found = {}
        for line in text.strip().splitlines():
            with contextlib.suppress(ValueError):
                j = json.loads(line)
                if isinstance(j, dict):
                    found.update({k: j[k] for k in result["fields"] if k in j})
        first = result["fields"][0]
        if empty(found.get(first)):
            found[first] = text.strip()
        return json.dumps({k: (found.get(k).strip() if isinstance(found.get(k), str) else found.get(k)) for k in result["fields"]})
    ext = result.get("ext") or got_name.rsplit(".", 1)[-1]
    extra, parts = fill(result.get("report", {}), env), []
    if result.get("preview"):
        img = PILImage.open(io.BytesIO(data))
        if "A" in img.getbands() and img.getchannel("A").getextrema()[0] >= 250:
            buf = io.BytesIO()  # the model writes an alpha channel even when nothing is transparent
            img.convert("RGB").save(buf, "PNG")
            data = buf.getvalue()
        info, thumb = preview(data)
        extra, parts = {**info, **extra}, [thumb]
    saved = new_name(result.get("save", name), ext)
    (FILES / saved).write_bytes(data)
    answer = json.dumps(describe(saved, getattr(ctx, "headers", None), seconds=round(time.time() - t0, 1), **extra))
    return [answer, *parts, *in_band(saved, data)]


def as_function(spec, path):
    """A tool file as the kind of function the MCP library takes: one named argument for each parameter."""
    async def call(ctx: Context | None = None, **args):
        return await execute(spec, path, args, ctx)

    P, sig = inspect.Parameter, []
    for name, p in spec.get("params", {}).items():
        kind = Literal[tuple(p["enum"])] if "enum" in p else PY_TYPES[p["type"]]
        note = p.get("description", "") + (FILE_NOTE if p["type"] in ("image", "images", "file") else "")
        if p.get("required"):
            default = P.empty
        elif p.get("default") in (None, "random"):
            kind, default = kind | None, None
        else:
            default = p["default"]
        mark = {"json_schema_extra": {"x-file": True}} if p["type"] in ("image", "images", "file") else {}  # for clients that send files themselves
        sig.append(P(name, P.KEYWORD_ONLY, default=default, annotation=Annotated[kind, Field(description=note.strip() or None, **mark)]))
    sig.append(P("ctx", P.KEYWORD_ONLY, default=None, annotation=Context | None))
    call.__signature__ = inspect.Signature(sig)
    call.__annotations__ = {p.name: p.annotation for p in sig}
    return call


# ---- tools that are always there ---------------------------------------
@mcp.tool(name="upload_link")
async def upload_link_tool(filename: Annotated[str, Field(description="The file on your computer, with its path, e.g. /Users/me/Pictures/cat.jpg")],
                           ctx: Context | None = None):
    """Get a file from your computer onto the server, so that the other tools can use it. Call this, run the
    command in the answer on the computer that has the file, then pass the `file` name from the answer to the
    tool that needs it. Not needed for a file an earlier tool of this server made, or for an http(s) link."""
    return json.dumps(upload_link(filename, getattr(ctx, "headers", None)))


@mcp.tool()
async def put_file(filename: Annotated[str, Field(description="What the file is called, e.g. cat.jpg")],
                   data_base64: Annotated[str, Field(description="The file's bytes, base64-encoded")]):
    """Send a file to the server inside this call, so that the other tools can use it. For clients that can pass a
    file's bytes; if you cannot, use upload_link. The answer gives the `file` name to pass on."""
    try:
        data = base64.b64decode(data_base64.split(",", 1)[-1], validate=True)
    except ValueError as e:
        raise ToolError("data_base64 is not base64.") from e
    ext = Path(filename).suffix.lstrip(".").lower() or "bin"
    name = new_name("upload", ext if ext.isalnum() and len(ext) <= 5 else "bin")
    (FILES / name).write_bytes(data)
    return json.dumps({"file": name, "size_kb": round(len(data) / 1024), "next": f'Pass "{name}" wherever a tool of this server takes a file.'})


@mcp.tool()
async def get_file(file: Annotated[str, Field(description="The `file` name from an earlier answer of this server")]):
    """Get a file from the server inside this call: the answer carries the file itself. Use it when your client has
    no download tool for the `url` of an answer."""
    p = FILES / Path(file).name
    if not p.is_file():
        raise ToolError(f"No file {file!r} on the server.")
    data = p.read_bytes()
    if len(data) > 32 << 20:
        raise ToolError(f"{file} is {len(data) >> 20} MB, too large to send inside an answer. Download its `url`.")
    blob = EmbeddedResource(resource=BlobResourceContents(uri=f"pinecone://files/{p.name}", mimeType=kind_of(p.name),
                                                           blob=base64.b64encode(data).decode()))
    return [json.dumps({"file": p.name, "size_kb": round(len(data) / 1024), "type": kind_of(p.name)}), blob]


@mcp.resource("pinecone://files/{name}", mime_type="application/octet-stream")
def stored_file(name: str) -> bytes:
    """A file a tool made or that was sent here, for clients that read resources."""
    p = FILES / Path(name).name
    if not p.is_file():
        raise ValueError(f"no file {name}")
    return p.read_bytes()


@mcp.tool()
async def media_status():
    """What the server has in memory right now and what is waiting. Use it to explain a long wait."""
    s = await Api.scheduler("/scheduler/status")
    return json.dumps({"in_memory_gb": s["in_memory_gb"], "budget_gb": s["budget_gb"],
                       "resources": {n: {k: x[k] for k in ("state", "active", "idle_s", "memory_gb")} for n, x in s["resources"].items()},
                       "queue": s["queue"]})


BUILT_IN = ("upload_link", "put_file", "get_file", "media_status")
LOADED = {"names": [], "errors": {}, "stamp": None, "files": []}


def tools_stamp():
    """Changes when the config, its .env or a file one of the tools names changes."""
    def when(f):
        with contextlib.suppress(OSError):
            return f.stat().st_mtime
    return [when(CONFIG), env_mtime(CONFIG)] + [when(CONFIG.parent / f) for f in LOADED["files"]]


def load_tools():
    """Make the tools the config describes. One with a mistake is left out and named in /health."""
    global M
    for name in LOADED["names"]:
        with contextlib.suppress(Exception):
            mcp.remove_tool(name)
    names, errors, files = [], {}, []
    try:
        whole, mine = read_config()
        M, specs = mine["models"], whole.get("tools", {})
        if not isinstance(specs, dict):
            raise ValueError('"tools" must be an object: a name for each tool, and its description')
    except (OSError, ValueError) as e:
        specs, errors = {}, {"config": str(e)}
    for name, spec in specs.items():
        try:
            spec = {"name": name, **spec}
            if name in BUILT_IN:
                raise ValueError("this name is taken by a tool that is always there")
            check_tool(spec, CONFIG)
            for run in [spec["run"]] + [v["run"] for v in spec.get("variants", []) if "run" in v]:
                files += [run["comfy"]] if isinstance(run.get("comfy"), str) else []
                files += [run["python"].partition(":")[0]] if "python" in run else []
            mcp.add_tool(as_function(spec, CONFIG), name=name, description=spec["description"])
            names.append(name)
        except Exception as e:  # noqa: BLE001
            errors[name] = str(e)
            print(f"tool {name} left out: {e}", file=sys.stderr, flush=True)
    LOADED.update(names=names, errors=errors, files=files)
    LOADED["stamp"] = tools_stamp()


def watch_tools():
    """Take up changes to the tools while running."""
    while True:
        time.sleep(2)
        if tools_stamp() != LOADED["stamp"]:
            load_tools()


# ---- plain HTTP: files -------------------------------------------------
@mcp.custom_route("/health", methods=["GET"])
async def health(request):
    return JSONResponse({"status": "ok", "tools": [*BUILT_IN, *LOADED["names"]], "tool_errors": LOADED["errors"]})


@mcp.custom_route("/files/{name}", methods=["GET"])
async def get_file(request):
    p = FILES / Path(request.path_params["name"]).name
    if not p.is_file():
        return JSONResponse({"error": "no such file"}, status_code=404)
    return FileResponse(p)


@mcp.custom_route("/upload", methods=["POST"])
async def upload(request):
    """Raw body, with ?name=photo.jpg, or a multipart form with a `file` field."""
    if request.headers.get("content-type", "").startswith("multipart/"):
        form = await request.form()
        f = form.get("file")
        if f is None:
            return JSONResponse({"error": "multipart form needs a `file` field"}, status_code=400)
        orig, data = f.filename or "upload.bin", await f.read()
    else:
        orig, data = request.query_params.get("name", "upload.bin"), await request.body()
    ext = Path(orig).suffix.lstrip(".").lower() or "bin"
    if not ext.isalnum() or len(ext) > 5:
        ext = "bin"
    name = new_name("upload", ext)
    (FILES / name).write_bytes(data)
    return JSONResponse(describe(name, request.headers))


@mcp.custom_route("/upload/{name}", methods=["PUT", "POST"])
async def upload_to(request):
    """Where an upload link points: the body is the file, and the name was fixed when the link was made."""
    name = Path(request.path_params["name"]).name
    (FILES / name).write_bytes(await request.body())
    return JSONResponse({"file": name, "size_kb": round((FILES / name).stat().st_size / 1024),
                         "next": f"Pass \"{name}\" wherever a tool of this server takes a file."})


TOKENS_OK = {}  # Authorization header -> time it was last confirmed


async def token_ok(header):
    """True for an access token made in the menu bar app: the scheduler knows those, so ask it (and remember for a minute)."""
    if not header.startswith("Bearer ") or len(header) > 300:
        return False
    if time.time() - TOKENS_OK.get(header, 0) < 60:
        return True
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            ok = (await c.get(SCHED + "/scheduler/whoami", headers={"Authorization": header})).status_code == 200
    except httpx.HTTPError:
        return False
    if ok:
        if len(TOKENS_OK) > 200:
            TOKENS_OK.clear()
        TOKENS_OK[header] = time.time()
    return ok


class BearerOnly:
    """Everything needs the scheduler's key or an access token, except /health and file links that carry a signature."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        linked = scope["type"] == "http" and scope["path"].startswith(("/files/", "/upload/")) and signature_ok(
            scope["path"], scope.get("query_string", b"").decode("latin-1"))
        if scope["type"] == "http" and scope["path"] != "/health" and not linked:
            got = dict(scope["headers"]).get(b"authorization", b"").decode("latin-1")
            if not hmac.compare_digest(got.encode("latin-1", "replace"), f"Bearer {KEY}".encode()) and not await token_ok(got):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b'{"error":"missing or wrong API key"}'})
                return
        await self.app(scope, receive, send)


def announce():
    """Tell the scheduler where clients find this server, so the menu bar app can show the address."""
    while True:
        with contextlib.suppress(httpx.HTTPError):
            httpx.post(f"{SCHED}/scheduler/announce", headers=AUTH, timeout=5,
                       json={"name": "Media tools (MCP)", "url": CFG["public_url"].rstrip("/") + "/mcp",
                             "info": {"tools": [*BUILT_IN, *LOADED["names"]], "tool_errors": LOADED["errors"]}})
        time.sleep(20)  # often enough to be listed again soon after the scheduler restarts


def tidy():
    while True:
        gone = sweep()
        if gone:
            print(f"deleted {gone} old file(s)", file=sys.stderr, flush=True)
        time.sleep(3600)


if __name__ == "__main__":
    load_tools()
    threading.Thread(target=tidy, daemon=True).start()
    threading.Thread(target=watch_tools, daemon=True).start()
    threading.Thread(target=announce, daemon=True).start()
    app = mcp.streamable_http_app(
        stateless_http=True, max_request_body_size=256 * 1024 * 1024,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                                     allowed_hosts=CFG["allowed_hosts"], allowed_origins=[]))
    uvicorn.run(BearerOnly(app), host=CFG["listen"]["host"], port=CFG["listen"]["port"], log_level="info")
