#!/usr/bin/env python3
"""Pinecone scheduler.

One address (port 8080) in front of every local AI service. It decides which
model or service may be in memory, starts and stops them on demand, and makes
requests wait in a queue while a swap is under way.

Rules
  * A resource that is working on a request is never stopped.
  * An idle resource is stopped to make room, after its keep-alive (time since
    its last request) and its minimum residency have passed; or after
    `idle_stop_s` without work, if it has one; or when asked to (unload).
  * A resource that needs as much as the whole budget gets it: everything else
    is off while it runs.
  * The oldest waiting request plans first. Once it has waited longer than
    `starvation_after_s`, whatever blocks it gets no new work and is stopped
    as soon as it is idle.

The config file is read again whenever it changes on disk (or on
POST /scheduler/reload). Nothing that is loaded is interrupted by that: a new
start command or port takes effect the next time that resource loads, and a
resource taken out of the file is stopped once it is idle.
"""
import asyncio
import contextlib
import ctypes
import hashlib
import hmac
import json
import logging
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

import re

import psutil
from aiohttp import ClientError, ClientSession, ClientTimeout, web

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from envfile import config_path, dump, env_mtime, expand, read_env  # noqa: E402
log = logging.getLogger("scheduler")

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "host"}


_libproc = None


def footprint_gb(pid):
    """Memory a process and its children really hold (macOS physical footprint, else RSS)."""
    global _libproc
    total = 0
    try:
        procs = [psutil.Process(pid)]
        procs += procs[0].children(recursive=True)
    except psutil.Error:
        return None
    for p in procs:
        size = 0
        if sys.platform == "darwin":
            try:
                if _libproc is None:
                    _libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
                buf = ctypes.create_string_buffer(1024)
                if _libproc.proc_pid_rusage(p.pid, 2, buf) == 0:  # RUSAGE_INFO_V2: ri_phys_footprint
                    size = int.from_bytes(buf.raw[72:80], "little")
            except OSError:
                pass
        try:
            size = max(size, p.memory_info().rss)
        except psutil.Error:
            pass
        total += size
    return round(total / 2 ** 30, 1)


async def gpu_percent():
    """Apple GPU utilisation, read from the IORegistry. None where that does not exist."""
    if sys.platform != "darwin":
        return None
    try:
        p = await asyncio.create_subprocess_exec("/usr/sbin/ioreg", "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator",
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(p.communicate(), 3)
        vals = [int(part.split("=")[1].split(",")[0].strip(" }")) for part in out.decode("utf-8", "replace").split('"Device Utilization %"')[1:]]
        return max(vals) if vals else None
    except (OSError, ValueError, IndexError, asyncio.TimeoutError):
        return None


def env_entries(path):
    """The NAME=value lines of a .env file, in order."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k, v = s.split("=", 1)
            out.append([k.removeprefix("export ").strip(), v.strip().strip("\"'")])
    return out


def env_text(path, changes):
    """A .env file with some entries set or (None) taken out; its comments and other lines stay."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        lines = []
    out, left = [], dict(changes)
    for line in lines:
        s = line.strip()
        name = s.split("=", 1)[0].removeprefix("export ").strip() if s and not s.startswith("#") and "=" in s else None
        if name in left:
            v = left.pop(name)
            if v is not None:
                out.append(f"{name}={v}")
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in left.items() if v is not None]
    return "\n".join(out) + "\n"


def write_file(path, text, private=False):
    """Replace a file in one step. A private file is never readable by others, not even for a moment."""
    tmp = Path(str(path) + ".tmp")
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644), "w") as f:
        f.write(text)
    os.replace(tmp, path)


LAUNCH_KEYS = ("cmd", "cwd", "port", "health", "start_timeout_s")


RAM_TOTAL_GB = round(psutil.virtual_memory().total / 2 ** 30, 1)
MIN_RESERVE_GB = 4
GPU_LIMIT_HELPER = "/usr/local/sbin/pinecone-gpu-limit"  # installed by install-gpu-limit.sh; sets iogpu.wired_limit_mb


def budget_of(cfg):
    """Memory the models may use: everything except what is reserved for the system."""
    if "system_reserve_gb" in cfg:
        return round(RAM_TOTAL_GB - cfg["system_reserve_gb"], 1)
    return cfg["memory_budget_gb"]


def merge(into, patch):
    """Put the values of patch into a nested dict; null removes a key."""
    for k, v in patch.items():
        if v is None:
            into.pop(k, None)
        elif isinstance(v, dict) and isinstance(into.get(k), dict):
            merge(into[k], v)
        else:
            into[k] = v


def load_config(path):
    """Read a config file: check it, fill in what it leaves out, and put the variables in ($NAME, see envfile.py).
    Each resource can also use $PORT (its own port) and $PYTHON (the Python this scheduler runs on)."""
    cfg = json.loads(Path(path).read_text())
    check_config(cfg)
    env = read_env(path)
    cfg["listen"] = {"host": "127.0.0.1", "port": 8080, **cfg.get("listen", {})}
    cfg["api_key_file"] = expand(cfg.get("api_key_file", "$CONFIG_DIR/internal-key.txt"), env)
    cfg["tokens_file"] = expand(cfg.get("tokens_file", "$CONFIG_DIR/tokens.json"), env)
    if "public_url" in cfg:
        cfg["public_url"] = expand(cfg["public_url"], env).rstrip("/")
    for n, c in cfg["resources"].items():
        own = {**env, "PORT": str(c["port"]), "PYTHON": sys.executable}
        try:
            c["cmd"] = expand(c["cmd"], own)
            if "cwd" in c:
                c["cwd"] = expand(c["cwd"], own)
        except ValueError as e:
            raise ValueError(f"{n}: {e}") from None
    return cfg


def check_config(cfg):
    """Raise ValueError with a readable message when a config cannot be used."""
    if not isinstance(cfg, dict) or not isinstance(cfg.get("resources"), dict) or not cfg["resources"]:
        raise ValueError('"resources" must be a non-empty object')
    if "system_reserve_gb" in cfg:
        v = cfg["system_reserve_gb"]
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v < MIN_RESERVE_GB or v > RAM_TOTAL_GB / 2:
            raise ValueError(f'"system_reserve_gb" must be a number from {MIN_RESERVE_GB} to {RAM_TOTAL_GB / 2:.0f}')
    elif not isinstance(cfg.get("memory_budget_gb"), (int, float)) or isinstance(cfg.get("memory_budget_gb"), bool):
        raise ValueError('give "system_reserve_gb" (memory kept free for macOS), or the older "memory_budget_gb"')

    def number(where, c, key, least=0):
        v = c.get(key)
        if v is not None and (not isinstance(v, (int, float)) or isinstance(v, bool) or v < least):
            raise ValueError(f'{where}"{key}" must be a number' + (f" of at least {least}" if least else ""))
    number("", cfg, "starvation_after_s")
    listen = cfg.get("listen", {})
    if not isinstance(listen, dict) or not isinstance(listen.get("host", ""), str) or not isinstance(listen.get("port", 0), int) \
            or isinstance(listen.get("port", 0), bool):
        raise ValueError('"listen" must be {"host": text, "port": whole number}')
    if not isinstance(cfg.get("public_url", ""), str):
        raise ValueError('"public_url" must be text')
    if not isinstance(cfg.get("manage_gpu_limit", True), bool):
        raise ValueError('"manage_gpu_limit" must be true or false')
    ports = {}
    for n, c in cfg["resources"].items():
        if not isinstance(c, dict):
            raise ValueError(f"{n}: must be an object")
        if c.get("type", c.get("kind", "llm")) not in ("llm", "service"):
            raise ValueError(f'{n}: "type" must be "llm" or "service"')
        if not isinstance(c.get("memory_gb"), (int, float)) or isinstance(c.get("memory_gb"), bool) or c["memory_gb"] <= 0:
            raise ValueError(f'{n}: "memory_gb" must be a number above 0')
        for key in ("keep_alive_s", "min_residency_s", "idle_stop_s", "start_timeout_s"):
            number(f"{n}: ", c, key)
        if not isinstance(c.get("port"), int) or isinstance(c.get("port"), bool):
            raise ValueError(f'{n}: "port" must be a whole number')
        if not isinstance(c.get("cmd"), list) or not c["cmd"] or not all(isinstance(a, str) for a in c["cmd"]):
            raise ValueError(f'{n}: "cmd" must be a list of strings')
        if c["port"] in ports:
            raise ValueError(f"{n} and {ports[c['port']]} use the same port {c['port']}")
        ports[c["port"]] = n


class Res:
    def __init__(self, name, cfg):
        self.name = name
        self.cfg = cfg
        self.port = cfg["port"]
        self.next_cfg = None   # launch settings that changed while loaded; used from the next load on
        self.retired = False   # taken out of the config file; goes away once it is off
        self.policy(cfg)
        self.state = "off"  # off | starting | ready | stopping
        self.proc = None
        self.logf = None
        self.active = 0
        self.last_used = 0.0
        self.loaded_at = 0.0
        self.started_at = 0.0
        self.error = None
        self.draining = False
        self.actual = None  # memory it really holds, measured while it runs
        self.run = 0        # counts its starts, so work from an earlier run cannot be booked on a later one

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}"

    def policy(self, cfg):
        """Settings that can change while the resource is loaded."""
        self.title = cfg.get("title", self.name)
        self.kind = cfg.get("type", cfg.get("kind", "llm"))  # "kind" is the older word for it
        self.mem = cfg["memory_gb"]
        self.keep = cfg.get("keep_alive_s", 300)
        self.min_res = cfg.get("min_residency_s", 60)
        self.idle_stop = cfg.get("idle_stop_s")

    def apply(self, cfg):
        """Take a new config. Returns True when part of it has to wait for the next load."""
        self.retired = False
        self.policy(cfg)
        if self.state == "off":
            self.cfg, self.port, self.next_cfg = cfg, cfg["port"], None
            return False
        current = self.next_cfg or self.cfg
        if any(cfg.get(k) != self.cfg.get(k) for k in LAUNCH_KEYS):
            self.next_cfg = cfg
            return any(cfg.get(k) != current.get(k) for k in LAUNCH_KEYS)
        self.cfg, self.next_cfg = cfg, None
        return False


class Tokens:
    """Access tokens made in the menu bar app. Only their hashes are kept, in tokens.json next to the config.
    A token may use the models and services; changing settings or tokens needs the scheduler's own key."""

    def __init__(self, path):
        self.path = path
        self.items = []
        self.saved = 0
        if path and path.exists():
            try:
                self.items = json.loads(path.read_text())
            except (OSError, ValueError):
                self.items = []

    def save(self):
        if not self.path:
            return
        write_file(self.path, json.dumps(self.items, indent=2) + "\n", private=True)
        self.saved = time.time()

    def check(self, token):
        h = hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()
        for it in self.items:
            if hmac.compare_digest(it["hash"], h):
                it["last_used"] = int(time.time())
                if time.time() - self.saved > 60:
                    self.save()
                return it["id"]
        return None

    def mint(self, name):
        token = "pc-" + secrets.token_urlsafe(32)
        it = {"id": secrets.token_hex(4), "name": name, "hash": hashlib.sha256(token.encode()).hexdigest(),
              "created": int(time.time()), "last_used": None}
        self.items.append(it)
        self.save()
        return it, token

    def revoke(self, tid):
        n = len(self.items)
        self.items = [it for it in self.items if it["id"] != tid]
        self.save()
        return len(self.items) < n

    def listing(self):
        now = time.time()
        return [{"id": it["id"], "name": it["name"], "created_ago_s": int(now - it["created"]),
                 "last_used_ago_s": int(now - it["last_used"]) if it.get("last_used") else None} for it in self.items]


class Scheduler:
    def __init__(self, cfg, key, path=None):
        self.cfg = cfg
        self.key = key
        self.tokens = Tokens(Path(cfg["tokens_file"]) if cfg.get("tokens_file") else None)
        self.path = path
        self.mtime = None
        self.mtime = self.stamp()
        self.loaded_at = time.time()
        self.config_error = None
        self.budget = budget_of(cfg)
        self.gpu_limit = {"managed": False, "wanted_mb": None, "error": None}
        self.gpu_retry = time.time()
        self.starve = cfg.get("starvation_after_s", 1800)
        self.res = {n: Res(n, c) for n, c in cfg["resources"].items()}
        self.waiters = []  # {"name", "fut", "t", "who"}
        self.leases = {}   # id -> {"resource", "expires", "who"}
        self.events = deque(maxlen=60)
        self.http = None
        self.tasks = set()
        self.host = deque(maxlen=150)  # one sample every 2 s: (cpu %, gpu %, ram used GB)
        self.gpu_now = None            # the GPU memory limit macOS has right now, in MB
        self.announced = {}            # name -> {"url", "t"}: addresses other services tell us about

    def stamp(self):
        """Changes when the config file or a .env file it uses changes."""
        if not self.path:
            return None
        try:
            return (self.path.stat().st_mtime, env_mtime(self.path))
        except OSError:
            return self.mtime

    # ---- GPU memory limit --------------------------------------------
    async def set_gpu_limit(self, mb):
        """Ask macOS to let the GPU wire up to mb megabytes (0 puts the default back). Needs the root helper."""
        g = self.gpu_limit
        g.update(managed=True, wanted_mb=mb)
        try:
            p = await asyncio.create_subprocess_exec("/usr/bin/sudo", "-n", GPU_LIMIT_HELPER, str(mb),
                                                     stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            _, e = await asyncio.wait_for(p.communicate(), 10)
            g["error"] = None if p.returncode == 0 else (
                "the helper is not installed: run scheduler/install-gpu-limit.sh once with sudo"
                if not os.path.exists(GPU_LIMIT_HELPER) or b"password" in e else e.decode(errors="replace").strip()[-200:])
        except (OSError, asyncio.TimeoutError) as e:
            g["error"] = str(e) or "timed out"
        text = (f"GPU memory limit not set: {g['error']}" if g["error"] else
                ("GPU memory limit back to the macOS default" if mb == 0 else f"GPU memory limit set to {mb / 1024:.0f} GB"))
        if text != g.get("said"):
            g["said"] = text
            self.note(text)

    def want_gpu_limit(self):
        """Megabytes the GPU may wire under the current config, or None when the scheduler should leave it alone."""
        if "system_reserve_gb" not in self.cfg or self.cfg.get("manage_gpu_limit") is False:
            return None
        return int(round(self.budget * 1024))

    async def sync_gpu_limit(self):
        mb = self.want_gpu_limit()
        if mb is None:
            if self.gpu_limit["managed"]:
                await self.set_gpu_limit(0)
                self.gpu_limit.update(managed=False, wanted_mb=None)
        elif mb != self.gpu_limit["wanted_mb"] or self.gpu_limit["error"]:
            await self.set_gpu_limit(mb)

    @staticmethod
    def gpu_limit_now():
        try:
            return int(subprocess.run(["/usr/sbin/sysctl", "-n", "iogpu.wired_limit_mb"], capture_output=True, text=True, timeout=2).stdout)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None

    def fail_waiters(self, name, why):
        for w in [w for w in self.waiters if w["name"] == name]:
            self.waiters.remove(w)
            if not w["fut"].done():
                w["fut"].set_exception(RuntimeError(why))

    # ---- bookkeeping -------------------------------------------------
    def note(self, text):
        log.info(text)
        self.events.appendleft({"t": time.time(), "text": text})

    def spawn(self, coro):
        t = asyncio.ensure_future(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)

    async def acquire(self, name, who=""):
        fut = asyncio.get_running_loop().create_future()
        w = {"name": name, "fut": fut, "t": time.time(), "who": who}
        self.waiters.append(w)
        self.step()
        try:
            return await fut  # which run of the resource this work belongs to
        except asyncio.CancelledError:
            if w in self.waiters:
                self.waiters.remove(w)
            elif fut.done() and not fut.cancelled() and fut.exception() is None:
                self.release(name, fut.result())
            raise

    def release(self, name, run):
        r = self.res.get(name)
        if r and r.run == run:  # not if it crashed and was started again in the meantime
            r.active = max(0, r.active - 1)
            r.last_used = time.time()
        self.step()

    # ---- config reload -----------------------------------------------
    def reload(self):
        """Read the config file again. Returns a list of what changed; raises ValueError if it is unusable."""
        try:
            cfg = load_config(self.path)
        except (OSError, ValueError) as e:
            self.config_error = str(e)
            self.note(f"config not reloaded: {e}")
            raise ValueError(str(e)) from None
        changes = []
        if budget_of(cfg) != self.budget:
            changes.append(f"memory for models {self.budget:g} -> {budget_of(cfg):g} GB")
        for k in ("listen", "api_key_file", "tokens_file"):
            if cfg.get(k) != self.cfg.get(k):
                changes.append(f"{k} changed: needs a scheduler restart")
        self.budget = budget_of(cfg)
        self.starve = cfg.get("starvation_after_s", 1800)
        taken = {x.port: x.name for x in self.res.values() if x.state != "off"}
        for n, c in cfg["resources"].items():
            r = self.res.get(n)
            if r is None:
                if taken.get(c["port"]):
                    changes.append(f"{n} added (its port {c['port']} is free once {taken[c['port']]} unloads)")
                else:
                    changes.append(f"{n} added")
                self.res[n] = Res(n, c)
            elif c != (r.next_cfg or r.cfg) or r.retired:
                changes.append(f"{n} changed" + (": start settings apply at its next load" if r.apply(c) else ""))
        for n, r in self.res.items():
            if n not in cfg["resources"] and not r.retired:
                r.retired = True
                changes.append(f"{n} removed" + ("" if r.state == "off" else ": stops once it is idle"))
        self.res = {n: self.res[n] for n in list(cfg["resources"]) + [n for n in self.res if n not in cfg["resources"]]}
        self.cfg = cfg
        self.config_error = None
        self.loaded_at = time.time()
        self.note("config reloaded: " + ("; ".join(changes) if changes else "no changes"))
        if self.http is not None and (self.want_gpu_limit() != self.gpu_limit["wanted_mb"]):
            self.spawn(self.sync_gpu_limit())
        self.step()
        return changes

    def save(self, patch, env=None):
        """Merge a patch into the config file (and entries into .env) and load the result. Raises ValueError and
        leaves the files alone if the config would be unusable."""
        try:
            cfg = json.loads(self.path.read_text())
        except (OSError, ValueError) as e:
            raise ValueError(f"cannot read the config file: {e}") from None
        merge(cfg, patch)
        if "system_reserve_gb" in cfg:
            cfg.pop("memory_budget_gb", None)
        check_config(cfg)
        for k, v in (env or {}).items():
            if not re.fullmatch(r"[A-Za-z_]\w*", k) or not (v is None or isinstance(v, str)) or "\n" in (v or ""):
                raise ValueError(f"{k!r} cannot be a variable: a name is letters, digits and _, a value one line of text")
        if env:
            write_file(self.path.parent / ".env", env_text(self.path.parent / ".env", env), private=True)
        write_file(self.path, dump(cfg) + "\n")
        self.mtime = self.stamp()
        return self.reload()

    def settle(self):
        """Apply what had to wait until a resource is off, and drop retired ones."""
        for n, r in list(self.res.items()):
            if r.retired:
                self.fail_waiters(n, f"{n} was removed from the configuration")
                if r.state == "ready" and r.active == 0:
                    r.state = "stopping"
                    self.note(f"stopping {n}: removed from the configuration")
                    self.spawn(self.stop(r))
                elif r.state == "off":
                    del self.res[n]
            elif r.state == "off" and r.next_cfg:
                r.cfg, r.port, r.next_cfg = r.next_cfg, r.next_cfg["port"], None

    # ---- the decision ------------------------------------------------
    def evictable(self, x, now):
        return (x.state == "ready" and x.active == 0 and now - x.loaded_at >= x.min_res
                and (x.draining or now - x.last_used >= x.keep))

    def evictions(self, r, now):
        """What has to be stopped before r fits."""
        loaded = [x for x in self.res.values() if x is not r and x.state != "off"]
        rest = sorted(loaded, key=lambda x: (not self.evictable(x, now), x.last_used))
        used = sum(self.need(x) for x in rest)
        ev = []
        for x in rest:
            if used + self.need(r) <= self.budget:
                break
            ev.append(x)
            used -= self.need(x)
        return ev

    def need(self, r):
        """Memory planned for r. Something that asks for more than there is gets all there is, and so runs alone."""
        return min(r.mem, self.budget)

    def step(self):
        now = time.time()
        for lid, lease in list(self.leases.items()):
            if lease["expires"] < now:
                del self.leases[lid]
                r = self.res.get(lease["resource"])
                if r and r.run == lease["run"]:
                    r.active = max(0, r.active - 1)
                    r.last_used = now
                self.note(f"lease on {lease['resource']} expired ({lease['who']})")
        self.settle()
        for name in {w["name"] for w in self.waiters} - set(self.res):
            self.fail_waiters(name, f"{name} was removed from the configuration")
        moving = any(r.state in ("starting", "stopping") for r in self.res.values())
        for r in self.res.values():
            r.draining = False
        planned = False
        for w in list(self.waiters):
            r = self.res[w["name"]]
            if r.state == "ready":
                if r.draining:
                    continue
                self.waiters.remove(w)
                r.active += 1
                r.last_used = now
                w["fut"].set_result(r.run)
                continue
            if r.state != "off" or planned:
                continue
            planned = True  # only the oldest request that needs a load plans
            ev = self.evictions(r, now)
            if now - w["t"] > self.starve:
                for x in ev:
                    x.draining = True
            if moving:
                continue
            if not ev:
                r.state = "starting"
                self.spawn(self.start(r))
            elif all(self.evictable(x, now) for x in ev):
                for x in ev:
                    x.state = "stopping"
                    self.note(f"stopping {x.name} to make room for {r.name}")
                    self.spawn(self.stop(x))
        wanted = {w["name"] for w in self.waiters}
        for r in self.res.values():
            if (r.state == "ready" and r.idle_stop and r.active == 0 and r.name not in wanted
                    and now - r.last_used > r.idle_stop):
                r.state = "stopping"
                self.note(f"stopping {r.name}: idle for {int(now - r.last_used)}s")
                self.spawn(self.stop(r))

    # ---- processes ---------------------------------------------------
    @staticmethod
    def port_busy(port):
        with socket.socket() as s:
            s.settimeout(0.5)
            return s.connect_ex(("127.0.0.1", port)) == 0

    async def start(self, r):
        r.started_at = time.time()
        self.note(f"starting {r.name}")
        try:
            if self.port_busy(r.port):
                raise RuntimeError(f"port {r.port} is already in use by another program")
            logs = (self.path.parent if self.path else HERE) / "logs"  # next to the config file
            logs.mkdir(exist_ok=True)
            r.logf = open(logs / f"{r.name}.log", "ab")
            r.proc = await asyncio.create_subprocess_exec(
                *r.cfg["cmd"], cwd=r.cfg.get("cwd"), stdout=r.logf, stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL, start_new_session=True)
            deadline = time.time() + r.cfg.get("start_timeout_s", 900)
            url = r.base + r.cfg.get("health", "/health")
            while True:
                if r.proc.returncode is not None:
                    raise RuntimeError(f"exited with code {r.proc.returncode} while starting; see logs/{r.name}.log")
                try:
                    async with self.http.get(url, timeout=ClientTimeout(total=3)) as resp:
                        if resp.status == 200:
                            break
                except (ClientError, asyncio.TimeoutError, OSError):
                    pass
                if time.time() > deadline:
                    raise RuntimeError("did not become ready in time")
                await asyncio.sleep(1)
            r.state = "ready"
            r.error = None
            r.run += 1
            r.loaded_at = r.last_used = time.time()
            self.note(f"{r.name} ready after {int(r.loaded_at - r.started_at)}s")
        except Exception as e:  # noqa: BLE001
            r.error = str(e)
            self.note(f"{r.name} failed to start: {e}")
            await self.kill(r)
            r.state = "off"
            self.fail_waiters(r.name, f"{r.name} failed to start: {e}")
        self.step()

    async def kill(self, r):
        p = r.proc
        if p is not None and p.returncode is None:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(p.wait(), 30)
            except asyncio.TimeoutError:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await p.wait()
        r.proc = None
        r.actual = None
        if r.logf:
            r.logf.close()
            r.logf = None

    async def crashed(self, r, code):
        """A server went away by itself: clear up after it, so it can be started again."""
        self.note(f"{r.name} exited unexpectedly (code {code})")
        r.error = f"exited unexpectedly (code {code})"
        for lid in [lid for lid, v in self.leases.items() if v["resource"] == r.name]:
            del self.leases[lid]
        try:
            os.killpg(r.proc.pid, signal.SIGKILL)  # anything it started and left behind would keep the port
        except (ProcessLookupError, PermissionError):
            pass
        await self.kill(r)
        r.active = 0
        r.state = "off"
        self.step()

    async def stop(self, r):
        await self.kill(r)
        r.state = "off"
        self.note(f"{r.name} stopped")
        self.step()

    async def watch(self):
        """Tick once a second, and notice services that died."""
        while True:
            await asyncio.sleep(1)
            try:  # one bad tick must not end the ticking
                self.tick()
            except Exception:  # noqa: BLE001
                log.exception("tick failed")

    def tick(self):
        for r in self.res.values():
            if r.state == "ready" and r.proc is not None and r.proc.returncode is not None:
                r.state = "stopping"
                self.spawn(self.crashed(r, r.proc.returncode))
        if self.path:
            m = self.stamp()
            if m != self.mtime:
                self.mtime = m
                try:
                    self.reload()
                except ValueError:
                    pass
        if self.gpu_limit["error"] and time.time() - self.gpu_retry > 20:  # e.g. the helper was just installed
            self.gpu_retry = time.time()
            self.spawn(self.sync_gpu_limit())
        self.step()

    async def sample(self):
        """Every 2 s: the machine's load, and what each running server really holds in memory."""
        psutil.cpu_percent(None)
        n = 0
        while True:
            await asyncio.sleep(2)
            try:
                vm = psutil.virtual_memory()
                self.host.append((round(psutil.cpu_percent(None)), await gpu_percent(), round((vm.total - vm.available) / 2 ** 30, 1)))
                for r in list(self.res.values()):
                    r.actual = footprint_gb(r.proc.pid) if r.proc is not None and r.proc.returncode is None else None
                if n % 5 == 0:
                    self.gpu_now = await asyncio.to_thread(self.gpu_limit_now)
                n += 1
            except Exception:  # noqa: BLE001
                log.exception("sampling failed")

    async def shutdown(self):
        for t in list(self.tasks):
            t.cancel()
        await asyncio.gather(*(self.kill(r) for r in self.res.values()))
        if self.gpu_limit["managed"] and not self.gpu_limit["error"]:
            await self.set_gpu_limit(0)

    def reason(self, w, now):
        """Why a request is still waiting, in words."""
        r = self.res.get(w["name"])
        if r is None:
            return "no longer in the configuration"
        if r.state == "starting":
            return f"{r.name} is loading"
        if r.state == "stopping":
            return f"{r.name} is still unloading"
        parts = []
        for x in self.evictions(r, now):
            if x.active:
                parts.append(f"{x.name} is working")
            elif x.state != "ready":
                parts.append(f"{x.name} is {x.state}")
            elif not self.evictable(x, now):
                left = max(x.keep - (now - x.last_used), x.min_res - (now - x.loaded_at))
                parts.append(f"{x.name} is kept for {int(left) + 1}s more")
            else:
                parts.append(f"{x.name} is unloading")
        return "waiting for: " + ", ".join(parts) if parts else "next in line"

    def endpoints(self, origin):
        """The addresses clients use: the models, and whatever other services have told us about themselves."""
        now = time.time()
        base, listen = self.cfg.get("public_url"), self.cfg["listen"]
        if not base:  # reachable from other machines: give the name they can use, else the address the caller used
            base = f"http://{socket.gethostname()}:{listen['port']}" if listen["host"] in ("0.0.0.0", "::") else origin
        return ([{"name": "Models (OpenAI-compatible)", "url": base + "/v1"}]
                + [{"name": n, "url": a["url"], **a.get("info", {})} for n, a in self.announced.items() if now - a["t"] < 180])

    def status(self):
        now = time.time()
        return {
            "budget_gb": self.budget,
            "system_reserve_gb": self.cfg.get("system_reserve_gb"),
            "gpu_limit": {"managed": self.gpu_limit["managed"], "wanted_mb": self.gpu_limit["wanted_mb"],
                          "error": self.gpu_limit["error"], "now_mb": self.gpu_now,
                          "installer": str(HERE / "install-gpu-limit.sh")},
            "config": {"path": str(self.path) if self.path else None, "loaded_ago_s": int(now - self.loaded_at),
                       "error": self.config_error},
            "in_memory_gb": sum(self.need(r) for r in self.res.values() if r.state != "off"),
            "resources": {
                n: {
                    "title": r.title, "kind": r.kind, "state": r.state, "memory_gb": r.mem,
                    "retired": r.retired, "pending_restart": r.next_cfg is not None,
                    "whole": r.mem >= self.budget, "active": r.active,
                    "actual_gb": r.actual,
                    "idle_s": int(now - r.last_used) if r.state == "ready" and r.active == 0 else None,
                    "loaded_s": int(now - r.loaded_at) if r.state == "ready" else None,
                    "keep_alive_s": r.keep, "idle_stop_s": r.idle_stop, "draining": r.draining, "error": r.error,
                } for n, r in self.res.items()},
            "host": {"interval_s": 2, "ram_total_gb": RAM_TOTAL_GB, "cpu_pct": [h[0] for h in self.host],
                     "gpu_pct": [h[1] for h in self.host], "ram_used_gb": [h[2] for h in self.host]},
            "queue": [{"resource": w["name"], "waiting_s": int(now - w["t"]), "who": w["who"],
                       "reason": self.reason(w, now)} for w in self.waiters],
            "leases": [{"resource": v["resource"], "who": v["who"], "expires_in_s": int(v["expires"] - now)}
                       for v in self.leases.values()],
            "events": [{"ago_s": int(now - e["t"]), "text": e["text"]} for e in self.events],
        }


# ---- HTTP --------------------------------------------------------------
def err(status, message, kind="scheduler_error"):
    return web.json_response({"error": {"message": message, "type": kind, "code": status}}, status=status)


def caller(request):
    """Who is asking: "admin" for the scheduler's own key, a token's id for an access token, None for neither."""
    sched = request.app["sched"]
    got = request.headers.get("Authorization", "")
    if hmac.compare_digest(got.encode("utf-8", "replace"), f"Bearer {sched.key}".encode()):
        return "admin"
    if got.startswith("Bearer "):
        return sched.tokens.check(got[7:])
    return None


def lease_time(j):
    """How long a lease lasts, in seconds: what the caller asks for, within reason."""
    try:
        ttl = float(j.get("ttl_s", 600))
    except (TypeError, ValueError):
        raise ValueError('"ttl_s" must be a number') from None
    if not 1 <= ttl <= 3600:
        raise ValueError('"ttl_s" must be between 1 and 3600')
    return ttl


def gone(request):
    return request.transport is None or request.transport.is_closing()


async def wait_for(request, sched, name, who, on_wait=None):
    """Wait for a resource; returns which run of it the caller now holds. Gives up when the caller goes away."""
    t = asyncio.ensure_future(sched.acquire(name, who))
    try:
        first = True
        while True:
            done, _ = await asyncio.wait([t], timeout=1.5 if first else 10)
            first = False
            if done:
                break
            if gone(request):
                raise ConnectionResetError("caller went away while waiting")
            if on_wait:
                await on_wait()
        return t.result()
    except BaseException:
        if not t.done():
            t.cancel()
        elif not t.cancelled() and t.exception() is None:
            sched.release(name, t.result())
        raise


async def proxy(request, r, body, stream):
    sched = request.app["sched"]
    resp = None

    async def on_wait():
        nonlocal resp
        if not stream:
            return
        if resp is None:
            resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream",
                                                           "Cache-Control": "no-cache"})
            await resp.prepare(request)
        await resp.write(f": waiting for {r.name}\n\n".encode())

    who = f"{request.remote} {request.method} {request.path}"
    def event(message, kind):
        return b"data: " + json.dumps({"error": {"message": message, "type": kind}}).encode() + b"\n\n"

    try:
        run = await wait_for(request, sched, r.name, who, on_wait)
    except (ConnectionResetError, asyncio.CancelledError):
        raise
    except Exception as e:  # noqa: BLE001
        if resp is None:
            return err(503, str(e))
        await resp.write(event(str(e), "scheduler_error"))
        return resp
    try:
        # the caller's key or token stays here: the servers behind listen on this Mac only and have none
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in HOP and k.lower() not in ("content-length", "authorization")}
        async with sched.http.request(request.method, r.base + request.path_qs, headers=headers,
                                      data=body if body else None, allow_redirects=False) as up:
            if resp is None:
                resp = web.StreamResponse(status=up.status, headers={
                    k: v for k, v in up.headers.items() if k.lower() not in HOP})
                await resp.prepare(request)
            elif up.status != 200:
                text = (await up.read()).decode("utf-8", "replace")
                try:
                    payload = json.loads(text)
                except ValueError:
                    payload = {"error": {"message": text, "type": "upstream_error", "code": up.status}}
                await resp.write(b"data: " + json.dumps(payload).encode() + b"\n\n")
                return resp
            async for chunk in up.content.iter_any():
                await resp.write(chunk)
            await resp.write_eof()
    except ClientError as e:
        if resp is None:
            return err(502, f"{r.name} did not answer: {e}")
        with contextlib.suppress(ConnectionError, RuntimeError):  # tell a client that is already reading a stream
            await resp.write(event(f"{r.name} stopped answering: {e}", "upstream_error"))
    finally:
        sched.release(r.name, run)
    return resp


async def models(request):
    sched = request.app["sched"]
    data = [{"id": n, "object": "model", "owned_by": "local", "created": 0,
             "meta": {"title": r.title, "state": r.state, "memory_gb": r.mem}}
            for n, r in sched.res.items() if r.kind == "llm" and not r.retired]
    return web.json_response({"object": "list", "data": data})


async def v1(request):
    sched = request.app["sched"]
    if caller(request) is None:
        return err(401, "Invalid API Key", "authentication_error")
    if request.method == "GET" and request.path.rstrip("/") == "/v1/models":
        return await models(request)
    body = await request.read()
    model, stream = None, False
    if body:
        try:
            j = json.loads(body)
            model, stream = j.get("model"), bool(j.get("stream"))
        except (ValueError, AttributeError):
            pass
    llms = [n for n, r in sched.res.items() if r.kind == "llm" and not r.retired]
    if model not in llms:
        return err(404, f"model {model!r} is not known here. Available: {', '.join(llms)}", "model_not_found")
    return await proxy(request, sched.res[model], body, stream)


async def passthrough(request):
    """Everything else (the llama.cpp web UI, /props, /slots) goes to the model used last."""
    sched = request.app["sched"]
    if caller(request) is None:
        return err(401, "Invalid API Key", "authentication_error")
    ready = [r for r in sched.res.values() if r.kind == "llm" and r.state == "ready"]
    if not ready:
        return err(503, "no language model is loaded right now; send a request to /v1/chat/completions to load one")
    r = max(ready, key=lambda x: x.last_used)
    return await proxy(request, r, await request.read(), False)


async def health(request):
    out = {"status": "ok"}
    if request.remote in ("127.0.0.1", "::1"):  # lets the menu bar app on this Mac find the key by itself
        out["key_file"] = request.app["sched"].cfg.get("api_key_file")
    return web.json_response(out)


async def api(request):
    sched = request.app["sched"]
    who = caller(request)
    if who is None:
        return err(401, "Invalid API Key", "authentication_error")
    op = request.match_info["op"]
    admin = who == "admin"
    reading = request.method == "GET"
    if op == "whoami":
        return web.json_response({"admin": admin})
    if op == "status":
        return web.json_response({**sched.status(), "endpoints": sched.endpoints(str(request.url.origin())), "admin": admin})
    if op in ("config", "reload", "tokens", "mint", "revoke", "announce") and not admin:
        return err(403, "this needs the scheduler's own key, not an access token", "permission_error")
    if op in ("config", "reload") and not sched.path:
        return err(409, "this scheduler was not started from a config file")
    if not reading and request.method != "POST":
        return err(405, "use GET or POST")
    if reading and op not in ("config", "tokens"):
        return err(405, f"{op} changes something: use POST")
    try:
        j = await request.json() if request.can_read_body else {}
    except ValueError:
        return err(400, "body must be JSON")
    if not isinstance(j, dict):
        return err(400, "body must be a JSON object")
    if op == "acquire":
        name = j.get("resource")
        if name not in sched.res or sched.res[name].retired:
            return err(404, f"unknown resource {name!r}")
        try:
            ttl = lease_time(j)
        except ValueError as e:
            return err(400, str(e))
        asker = str(j.get("who") or request.remote)
        try:
            run = await wait_for(request, sched, name, asker)
        except (ConnectionResetError, asyncio.CancelledError):
            raise
        except Exception as e:  # noqa: BLE001
            return err(503, str(e))
        lid = secrets.token_hex(12)
        sched.leases[lid] = {"resource": name, "expires": time.time() + ttl, "who": asker, "run": run}
        return web.json_response({"lease": lid, "resource": name, "url": sched.res[name].base, "ttl_s": ttl})
    if op in ("renew", "release"):
        lease = sched.leases.get(j.get("lease"))
        if not lease:
            return err(404, "no such lease (it may have expired)")
        if op == "renew":
            try:
                lease["expires"] = time.time() + lease_time(j)
            except ValueError as e:
                return err(400, str(e))
            return web.json_response({"ok": True})
        del sched.leases[j["lease"]]
        sched.release(lease["resource"], lease["run"])
        return web.json_response({"ok": True})
    if op == "announce":  # another service on this Mac says where clients find it; it repeats that to stay listed
        name, url = str(j.get("name") or "").strip()[:60], str(j.get("url") or "").strip()[:300]
        if not name or not url.startswith(("http://", "https://")):
            return err(400, 'send {"name": ..., "url": "http(s)://..."}')
        sched.announced[name] = {"url": url, "t": time.time(), "info": j.get("info") if isinstance(j.get("info"), dict) else {}}
        return web.json_response({"ok": True})
    if op == "config":
        if not reading:
            if not isinstance(j.get("patch", {}), dict) or not isinstance(j.get("env", {}), dict) or not ("patch" in j or "env" in j):
                return err(400, 'send {"patch": {...}} with the settings to change, and/or {"env": {...}} with variables')
            try:
                changes = sched.save(j.get("patch", {}), j.get("env"))
            except ValueError as e:
                return err(400, f"not saved: {e}")
            return web.json_response({"ok": True, "changes": changes})
        try:
            cfg = json.loads(sched.path.read_text())
        except (OSError, ValueError) as e:
            return err(500, f"cannot read the config file: {e}")
        return web.json_response({"config": cfg, "order": list(cfg.get("resources", {})),
                                  "env": env_entries(sched.path.parent / ".env")})
    if op == "tokens":
        return web.json_response({"tokens": sched.tokens.listing()})
    if op == "mint":
        name = str(j.get("name") or "").strip()[:60]
        if not name:
            return err(400, "give the token a name")
        it, token = sched.tokens.mint(name)
        sched.note(f"access token made: {name}")
        return web.json_response({"id": it["id"], "name": name, "token": token})
    if op == "revoke":
        if not sched.tokens.revoke(j.get("id")):
            return err(404, "no such token")
        sched.note("access token revoked")
        return web.json_response({"ok": True})
    if op == "reload":
        try:
            return web.json_response({"ok": True, "changes": sched.reload()})
        except ValueError as e:
            return err(400, f"config not reloaded, the old one stays in use: {e}")
    if op == "unload":
        r = sched.res.get(j.get("resource"))
        if not r:
            return err(404, "unknown resource")
        if r.state != "ready" or r.active:
            return err(409, f"{r.name} is {r.state} with {r.active} request(s) in progress")
        r.state = "stopping"
        sched.note(f"stopping {r.name}: asked to")
        sched.spawn(sched.stop(r))
        return web.json_response({"ok": True})
    return err(404, "unknown operation")


async def on_startup(app):
    sched = app["sched"]
    sched.http = ClientSession(timeout=ClientTimeout(total=None), auto_decompress=False)
    app["watch"] = asyncio.ensure_future(sched.watch())
    app["sample"] = asyncio.ensure_future(sched.sample())
    await sched.sync_gpu_limit()


async def on_cleanup(app):
    app["watch"].cancel()
    app["sample"].cancel()
    await app["sched"].shutdown()
    await app["sched"].http.close()


@web.middleware
async def quiet_disconnects(request, handler):
    try:
        return await handler(request)
    except ConnectionResetError:
        return web.Response(status=499)


def build(cfg, path=None):
    kf = Path(cfg["api_key_file"])
    if not kf.exists():  # the scheduler's own key: for the menu bar app and tools on this Mac
        write_file(kf, secrets.token_urlsafe(32) + "\n", private=True)
    key = kf.read_text().strip()
    if not key:
        raise SystemExit(f"{kf} is empty: delete it and a new key is made")
    app = web.Application(client_max_size=1024 ** 3, middlewares=[quiet_disconnects])
    app["sched"] = Scheduler(cfg, key, path)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_get("/health", health)
    app.router.add_route("*", "/scheduler/{op}", api)
    app.router.add_route("*", "/v1/{tail:.*}", v1)
    app.router.add_route("*", "/{tail:.*}", passthrough)
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    path = config_path(sys.argv)
    cfg = load_config(path)
    web.run_app(build(cfg, path), host=cfg["listen"]["host"], port=cfg["listen"]["port"], access_log=None)
