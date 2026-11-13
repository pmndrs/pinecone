"""Config reload test. Starts its own scheduler on a copy of tests/config.json and edits the copy."""
import asyncio, json, os, socket, subprocess, sys, tempfile, time
from pathlib import Path
import aiohttp
HERE = Path(__file__).resolve().parent
B = "http://127.0.0.1:18080"; H = {"Authorization": "Bearer test-key"}
ok = True
def check(label, cond, extra=""):
    global ok; ok &= bool(cond); print("PASS " if cond else "FAIL ", label, extra)
async def status(s):
    async with s.get(B + "/scheduler/status", headers=H) as r: return await r.json()
async def chat(s, model):
    async with s.post(B + "/v1/chat/completions", headers=H, json={"model": model}) as r: return r.status, await r.text()
async def main():
    cfg = json.loads((HERE / "config.json").read_text().replace("$CONFIG_DIR", str(HERE)))  # the copy lives elsewhere
    path = Path(tempfile.mkdtemp()) / "config.json"
    def save(c=None, raw=None):
        path.write_text(raw if raw is not None else json.dumps(c)); os.utime(path, (time.time(), time.time() + save.n)); save.n += 1
    save.n = 1
    path.write_text(json.dumps(cfg))
    p = subprocess.Popen([sys.executable, str(HERE.parent / "scheduler.py"), str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        async with aiohttp.ClientSession() as s:
            for _ in range(50):
                try:
                    async with s.get(B + "/health") as r: break
                except aiohttp.ClientError: await asyncio.sleep(0.2)
            st, _ = await chat(s, "mid"); check("mid loads", st == 200)
            # add one, remove an idle-off one, change memory of a loaded one, move the loaded one's port
            c = json.loads(json.dumps(cfg))
            c["resources"]["extra"] = dict(cfg["resources"]["small"], port=18106, title="Extra",
                                           cmd=cfg["resources"]["small"]["cmd"][:2] + ["extra", "18106"])
            del c["resources"]["broken"]
            c["resources"]["mid"]["memory_gb"] = 40
            c["resources"]["mid"]["port"] = 18107
            c["resources"]["mid"]["cmd"] = cfg["resources"]["mid"]["cmd"][:2] + ["mid", "18107"]
            save(c); await asyncio.sleep(2.5)
            j = await status(s); res = j["resources"]
            check("file change is picked up by itself", "extra" in res and "broken" not in res, str(list(res)))
            check("loaded model keeps running, new memory size applies", res["mid"]["state"] == "ready" and res["mid"]["memory_gb"] == 40)
            check("port change waits for next load", res["mid"]["pending_restart"] is True)
            st, tx = await chat(s, "mid"); check("mid still answers on its old port", st == 200 and '"mid"' in tx)
            st, tx = await chat(s, "extra"); check("added model works", st == 200 and '"extra"' in tx, tx[:60])
            st, _ = await chat(s, "broken"); check("removed model is 404", st == 404)
            async with s.post(B + "/scheduler/unload", headers=H, json={"resource": "mid"}) as r: pass
            await asyncio.sleep(2)
            st, tx = await chat(s, "mid"); j = await status(s)
            check("after reloading, mid runs with the new port", st == 200 and j["resources"]["mid"]["pending_restart"] is False)
            with socket.socket() as k: check("new port is the one in use", k.connect_ex(("127.0.0.1", 18107)) == 0)
            # broken file: old config stays
            save(raw='{"resources": '); await asyncio.sleep(2.5)
            j = await status(s)
            check("broken file is reported and ignored", j["config"]["error"] and "mid" in j["resources"], str(j["config"]["error"])[:60])
            async with s.post(B + "/scheduler/reload", headers=H) as r: check("reload endpoint says 400 for a broken file", r.status == 400)
            # remove a loaded one: stops when idle, then disappears
            c2 = json.loads(json.dumps(c)); del c2["resources"]["mid"]
            save(c2)
            async with s.post(B + "/scheduler/reload", headers=H) as r:
                jj = await r.json(); check("reload endpoint lists changes", r.status == 200 and any("mid removed" in x for x in jj["changes"]), str(jj))
            await asyncio.sleep(3); j = await status(s)
            check("removed loaded model is stopped and gone, error cleared", "mid" not in j["resources"] and j["config"]["error"] is None)
            # duplicate port is refused
            c3 = json.loads(json.dumps(c2)); c3["resources"]["small"]["port"] = 18106
            save(c3); await asyncio.sleep(2.5); j = await status(s)
            check("duplicate port is refused", j["config"]["error"] and "same port" in j["config"]["error"])
    finally:
        p.terminate(); p.wait()
    print("ALL PASS" if ok else "SOME FAILED")
    sys.exit(0 if ok else 1)
asyncio.run(main())
