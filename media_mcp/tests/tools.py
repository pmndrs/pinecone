"""Checks every tool in config.example.json without any service running: it loads, and a call with only
the required arguments comes to a complete request. Works on a copy of the example, so it runs on a fresh checkout.

    python media_mcp/tests/tools.py"""
import asyncio, importlib.util, json, shutil, sys, tempfile
from pathlib import Path
HERE = Path(__file__).resolve().parent
HOME = Path(tempfile.mkdtemp())  # a user's folder, as the first start would make it
shutil.copy(HERE.parents[1] / "config.example.json", HOME / "config.json")
shutil.copy(HERE.parents[1] / ".env.example", HOME / ".env")
(HOME / "internal-key.txt").write_text("test-key\n")
sys.argv = [sys.argv[0], str(HOME / "config.json")]
spec = importlib.util.spec_from_file_location("server", HERE.parent / "server.py")
server = importlib.util.module_from_spec(spec); spec.loader.exec_module(server)
ok = True
def check(label, cond, extra=""):
    global ok; ok &= bool(cond); print("PASS " if cond else "FAIL ", label, extra)
async def staged(ref, ctx=None): return "staged-" + Path(ref).name
async def fetched(ref, ctx=None): return b"bytes"
server.stage_image, server.fetch_bytes = staged, fetched
SAMPLE = {"string": "a test", "integer": 3, "number": 3.0, "boolean": True, "image": "in.png", "images": ["a.png"], "file": "in.wav"}

def leftovers(o):
    """Any "$name" still standing in a finished request."""
    if isinstance(o, dict): return [x for k, v in o.items() for x in leftovers(k) + leftovers(v)]
    if isinstance(o, list): return [x for v in o for x in leftovers(v)]
    return [o] if isinstance(o, str) and server.VAR.search(o) and o not in SAMPLE.values() else []

async def main():
    f = HOME / "config.json"
    tools = json.loads(f.read_text())["tools"]
    check("the example config comes with tools", len(tools) >= 7, str(list(tools)))
    for name, t in tools.items():
        t = {"name": name, **t}
        try:
            server.check_tool(t, f); fn = server.as_function(t, f); good = True
        except Exception as e:  # noqa: BLE001
            good = False; print("     ", e)
        check(f"{name} loads", good)
        if not good: continue
        args = {n: SAMPLE[p["type"]] for n, p in t.get("params", {}).items() if p.get("required")}
        p = await server.plan(t, f, args, None, [])
        request = p.get("comfy") or p.get("json") or p.get("form") or p.get("python")
        check(f"{t['name']} with only what is required comes to a request", bool(request) and not leftovers([request, server.fill(p["result"].get("report", {}), p["env"])]),
              str(leftovers(request))[:80])
        every = {n: (p_["enum"][-1] if "enum" in p_ else SAMPLE[p_["type"]]) for n, p_ in t.get("params", {}).items()}
        p = await server.plan(t, f, every, None, [])
        check(f"{t['name']} with every argument given", not leftovers([p.get("comfy"), p.get("json"), p.get("form")]))
    # the pieces of the format
    env = {"a": 2, "s": "x", "items": ["p", "q"], "t": True, "none": ""}
    check("a lone $name keeps its type, inside text it becomes text", server.fill({"n": "$a", "k$a": "v-$s-$$"}, env) == {"n": 2, "k2": "v-x-$"})
    check("a caller's $ is left alone", server.fill("$s", {"s": "costs $a"}) == "costs $a")
    check("map, else, prefix and times", server.value_of({"from": "$s", "map": {"x": 3}, "times": "$a"}, env) == 6
          and server.value_of({"from": "pt-br", "map": {"pt": "P"}, "prefix": "-"}, env) == "P"
          and server.value_of({"from": "$t", "map": {"true": 1}, "else": 0}, env) == 1)
    check("first and list", server.value_of({"first": ["$none", "$s"]}, env) == "x" and server.value_of({"list": ["$s", "$items", "$none"]}, env) == ["x", "p", "q"])
    check("pick is the same for the same seed", server.value_of({"pick": list("abcdef")}, {"seed": 5}) == server.value_of({"pick": list("abcdef")}, {"seed": 5}))
    check("when: equal, in, given", server.applies({"s": "x", "a": {"in": [1, 2]}, "none": {"given": False}, "t": True}, env) and not server.applies({"none": {"given": True}}, env))
    bad = {"name": "x", "description": "d", "service": "comfy", "run": {"post": "/p", "json": {"a": "$nope"}}}
    try: server.check_tool(bad, f); caught = False
    except ValueError as e: caught = "$nope" in str(e)
    check("an unknown $name is found when the config is loaded", caught)
    server.load_tools()
    check("the server makes all of them, next to its own", set(server.LOADED["names"]) == set(tools) and not server.LOADED["errors"], str(server.LOADED["errors"]))
    cfg = json.loads(f.read_text()); cfg["tools"]["mine"] = {"description": "d", "service": "comfy", "run": {"post": "/x", "json": {"a": "$nope"}}}
    cfg["tools"]["upload_link"] = {"description": "d", "run": {"python": "x.py:f"}}
    f.write_text(json.dumps(cfg)); server.load_tools()
    check("a tool with a mistake is left out and named, the others stay", set(server.LOADED["errors"]) == {"mine", "upload_link"} and len(server.LOADED["names"]) == len(tools), str(server.LOADED["errors"]))
    import os, time
    old, new, foreign = server.FILES / "image-20200101-000000-0123abcd.png", server.FILES / server.new_name("image", "png"), server.FILES / "holiday.png"
    for f in (old, new, foreign): f.write_bytes(b"x")
    for f in (old, foreign): os.utime(f, (time.time() - 30 * 86400,) * 2)
    check("old results are deleted, new ones and other people's files stay", server.sweep() == 1 and not old.exists() and new.exists() and foreign.exists())
    server.COMFY_OUT = HOME / "out"; (HOME / "out" / "mcp").mkdir(parents=True); (HOME / "out" / "mcp" / "a.png").write_bytes(b"x")
    server.drop_comfy_copy({"filename": "a.png", "subfolder": "mcp", "type": "output"}); server.drop_comfy_copy({"filename": "config.json", "subfolder": "..", "type": "output"})
    check("ComfyUI's copy of a result is removed, and nothing outside its folder", not (HOME / "out" / "mcp" / "a.png").exists() and f.exists() and (HOME / "config.json").exists())
    import httpx
    sent, real = [], httpx.AsyncClient
    def answer(request):  # a scheduler and a ComfyUI whose job never ends
        sent.append((request.method, request.url.path, json.loads(request.content or b"null")))
        return httpx.Response(200, json={"prompt_id": "job1", "lease": "lease1", "url": "http://comfy"})
    server.httpx.AsyncClient = lambda **kw: real(transport=httpx.MockTransport(answer), **{k: v for k, v in kw.items() if k != "transport"})
    async def call():
        async with server.lease("comfy", "a tool") as base:
            await server.run_comfy(base, {}, None, "a tool")
    with __import__("anyio").move_on_after(1.5):  # cancelled the way the MCP library does it: every later await fails
        await call()
    server.httpx.AsyncClient = real
    check("a call whose client went away stops its job and gives the service back",
          ("POST", "/interrupt", {"prompt_id": "job1"}) in sent and ("POST", "/queue", {"delete": ["job1"]}) in sent
          and ("POST", "/scheduler/release", {"lease": "lease1"}) in sent, str([x[1] for x in sent]))
    print("ALL PASS" if ok else "SOME FAILED"); sys.exit(0 if ok else 1)
asyncio.run(main())
