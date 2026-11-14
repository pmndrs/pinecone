"""What a model has read is kept across a stop ("keep_cache"). Starts its own scheduler on a copy of tests/config.json."""
import asyncio, json, subprocess, sys, tempfile
from pathlib import Path
import aiohttp
HERE = Path(__file__).resolve().parent
B = "http://127.0.0.1:18080"; H = {"Authorization": "Bearer test-key"}
ok = True
def check(label, cond, extra=""):
    global ok; ok &= bool(cond); print("PASS " if cond else "FAIL ", label, extra)
async def chat(s, model, **more):
    async with s.post(B + "/v1/chat/completions", headers=H, json={"model": model, **more}) as r: return r.status, await r.text()
async def states(s):
    async with s.get(B + "/scheduler/status", headers=H) as r: j = await r.json()
    return {n: x["state"] for n, x in j["resources"].items()}
async def slots(s):
    async with s.get(B + "/slots", headers=H) as r: return await r.json()  # goes to the model used last
async def main():
    cfg = json.loads((HERE / "config.json").read_text().replace("$CONFIG_DIR", str(HERE)))
    cfg["resources"]["mid"]["keep_cache"] = True
    cfg["resources"]["mid"]["cmd"] += ["--slow-restore", "1.3"]  # as a real server with a slot of some GB
    home = Path(tempfile.mkdtemp()).resolve()
    path = home / "config.json"
    path.write_text(json.dumps(cfg))
    kept = home / "cache" / "mid"
    p = subprocess.Popen([sys.executable, str(HERE.parent / "scheduler.py"), str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        async with aiohttp.ClientSession() as s:
            for _ in range(50):
                try:
                    async with s.get(B + "/health") as r: break
                except aiohttp.ClientError: await asyncio.sleep(0.2)
            st, _ = await chat(s, "mid", remember="a long conversation", slot=0); check("mid loads and reads something", st == 200)
            await chat(s, "mid", remember="another one", slot=1)
            j = await slots(s)
            check("it was started with a folder for its slots", j[0]["save_dir"] == str(kept), str(j[0]["save_dir"]))
            # small does not fit next to mid: mid is stopped, and what it had read is on disk
            st, _ = await chat(s, "small", remember="small's own"); stt = await states(s)
            check("mid is stopped to make room", st == 200 and stt["mid"] == "off" and stt["small"] == "ready")
            check("what mid had read is kept", sorted(f.name for f in kept.iterdir()) == ["slot-0.bin", "slot-1.bin", "stamp"], str(sorted(f.name for f in kept.iterdir())))
            j = await slots(s)
            check("a model without keep_cache is started as before", j[0]["save_dir"] is None)
            # mid comes back: it has what it had read, and the files are used up
            st, _ = await chat(s, "mid"); j = await slots(s)
            check("mid has back what it had read", st == 200 and [x["remembers"] for x in j] == ["a long conversation", "another one"], str(j))
            # The request that brings a model back waits for all its slots: one that was let through after the first slot
            # would be answered from an empty second slot, and what it left there be overwritten by what comes back late.
            await chat(s, "small")
            st, _ = await chat(s, "mid", remember="said by the request that brought it back", slot=1); j = await slots(s)
            check("a request that waits for the model gets in after every slot is back", st == 200 and [x["remembers"] for x in j] == ["a long conversation", "said by the request that brought it back"], str(j))
            check("the files are used up", not any(kept.iterdir()), str(list(kept.iterdir())))
            # small was stopped without keep_cache: nothing of it is kept, and it comes back empty
            st, _ = await chat(s, "small"); j = await slots(s)
            check("a model without keep_cache comes back empty", st == 200 and j[0]["remembers"] == "" and not (home / "cache" / "small").exists())
            # a model that is started differently does not get what the other one had read
            await chat(s, "mid", remember="read by the old command", slot=0)
            c = json.loads(json.dumps(cfg)); c["resources"]["mid"]["cmd"] = cfg["resources"]["mid"]["cmd"] + ["--changed"]
            await chat(s, "small")  # stops mid, which keeps its slot
            path.write_text(json.dumps(c))
            async with s.post(B + "/scheduler/reload", headers=H) as r: pass
            st, _ = await chat(s, "mid"); j = await slots(s)
            check("a changed command starts empty", st == 200 and j[0]["remembers"] == "" and not any(kept.iterdir()), str(j))
    finally:
        p.terminate(); p.wait()
    print("ALL PASS" if ok else "SOME FAILED")
    sys.exit(0 if ok else 1)
asyncio.run(main())
