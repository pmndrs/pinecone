"""Where Pinecone keeps a user's things, and the variables its config file can use.

Everything that belongs to a user is in one folder, ~/.pinecone (or $PINECONE_HOME): config.json, .env, the
scheduler's key, access tokens, logs and the files the media tools make. The program's own folder holds
none of it. The first start puts a config.json and a .env there, copied from the examples that come with
the program.

Variables are written the way pine writes them: $NAME or ${NAME}, and $$ for a dollar sign. Values come
from the environment and from the `.env` file next to the config file; the environment wins.
"""
import os
import re
import shutil
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parent


def config_path(argv):
    """The config file to use: the one named on the command line, else the user's. Makes the user's on first start."""
    if len(argv) > 1:
        return Path(argv[1]).resolve()
    home = Path(os.environ.get("PINECONE_HOME", "~/.pinecone")).expanduser()
    path = home / "config.json"
    if not path.exists():
        home.mkdir(parents=True, exist_ok=True)
        shutil.copy(SOURCE / "config.example.json", path)
        if not (home / ".env").exists():
            shutil.copy(SOURCE / ".env.example", home / ".env")
        print(f"First start: made {path} and {home / '.env'} from the examples. Put your own paths and models in.",
              file=sys.stderr, flush=True)
    return path

VAR = re.compile(r"\$(?:\$|\{(\w+)\}|(\w+))")


def read_env(path):
    """The variables a config file may use, plus CONFIG_DIR (the folder the config file is in)."""
    folder = Path(path).resolve().parent
    out = {}
    try:
        lines = (folder / ".env").read_text().splitlines()
    except OSError:
        lines = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) > 1 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k.removeprefix("export ").strip()] = v
    return {**out, **os.environ, "CONFIG_DIR": str(folder)}


def env_mtime(path):
    """Changes whenever the .env file changes, so a running service can notice."""
    try:
        return (Path(path).resolve().parent / ".env").stat().st_mtime
    except OSError:
        return 0


def expand(value, env):
    """Fill the variables into a string, or into every string of a list or dict. An unknown name is an error."""
    if isinstance(value, list):
        return [expand(v, env) for v in value]
    if isinstance(value, dict):
        return {k: expand(v, env) for k, v in value.items()}
    if not isinstance(value, str):
        return value

    def sub(m):
        if m.group(0) == "$$":
            return "$"
        name = m.group(1) or m.group(2)
        if name not in env:
            raise ValueError(f"${name} is used in the config but not set; put it in a .env file next to the config")
        return env[name]
    return VAR.sub(sub, value)


def dump(value, level=0, width=116):
    """JSON for a config file, kept short enough to read: whatever fits on one line stays on one line, and a long
    list of plain values (a start command) is wrapped like text. Otherwise it is indented like json.dumps(indent=2)."""
    import json
    pad = "  " * level
    flat = json.dumps(value, ensure_ascii=False)
    if not isinstance(value, (dict, list)) or not value or len(flat) + len(pad) <= width - 16:
        return flat
    if isinstance(value, dict):
        return "{\n" + ",\n".join(f"{pad}  {json.dumps(k, ensure_ascii=False)}: {dump(v, level + 1, width)}" for k, v in value.items()) + f"\n{pad}}}"
    if all(not isinstance(v, (dict, list)) for v in value):
        lines, line = [], ""
        for item in (json.dumps(v, ensure_ascii=False) for v in value):
            if line and len(pad) + 2 + len(line) + 2 + len(item) > width:
                lines.append(line)
                line = ""
            line = f"{line}, {item}" if line else item
        return "[\n" + ",\n".join(f"{pad}  {ln}" for ln in lines + [line]) + f"\n{pad}]"
    return "[\n" + ",\n".join(f"{pad}  {dump(v, level + 1, width)}" for v in value) + f"\n{pad}]"
