"""Settings test: system reserve, reading and saving the config, access tokens, what needs a key, leases with a bad
lifetime, announced endpoints, and the GPU limit report. Starts its own scheduler on port 18080."""
import asyncio, json, subprocess, sys, tempfile
from pathlib import Path
import aiohttp, psutil
HERE = Path(__file__).resolve().parent
B = "http://127.0.0.1:18080"; H = {"Authorization": "Bearer test-key"}
TOTAL = round(psutil.virtual_memory().total / 2 ** 30, 1)
ok = True
def check(label, cond, extra=""):
    global ok; ok &= bool(cond); print("PASS " if cond else "FAIL ", label, extra)
async def main():
    cfg = json.loads((HERE / "config.json").read_text().replace("$CONFIG_DIR", str(HERE)))  # the copy lives elsewhere
    del cfg["memory_budget_gb"]; cfg["system_reserve_gb"] = 10; cfg["manage_gpu_limit"] = False
    for c in cfg["resources"].values(): c["memory_gb"] = 1
    path = Path(tempfile.mkdtemp()) / "config.json"; path.write_text(json.dumps(cfg, indent=2))
    p = subprocess.Popen([sys.executable, str(HERE.parent / "scheduler.py"), str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        async with aiohttp.ClientSession() as s:
            for _ in range(50):
                try:
                    async with s.get(B + "/health") as r: break
                except aiohttp.ClientError: await asyncio.sleep(0.2)
            async def status():
                async with s.get(B + "/scheduler/status", headers=H) as r: return await r.json()
            async def post(patch):
                async with s.post(B + "/scheduler/config", headers=H, json={"patch": patch}) as r: return r.status, await r.json()
            j = await status()
            check("budget is total memory minus the reserve", j["budget_gb"] == round(TOTAL - 10, 1), str(j["budget_gb"]))
            check("GPU limit is left alone when told so", j["gpu_limit"]["managed"] is False and j["gpu_limit"]["error"] is None)
            async with s.get(B + "/scheduler/config") as r: check("config needs the key", r.status == 401)
            async with s.get(B + "/scheduler/config", headers=H) as r: g = await r.json()
            check("config is returned with order", g["order"] == list(cfg["resources"]) and g["config"]["system_reserve_gb"] == 10)
            st, a = await post({"system_reserve_gb": 12, "resources": {"mid": {"memory_gb": 2, "keep_alive_s": 9, "title": "Middle"}}})
            j = await status(); disk = json.loads(path.read_text())
            check("save applies at once", st == 200 and j["budget_gb"] == round(TOTAL - 12, 1) and j["resources"]["mid"]["memory_gb"] == 2
                  and j["resources"]["mid"]["keep_alive_s"] == 9 and j["resources"]["mid"]["title"] == "Middle", str(a))
            check("save writes the file and keeps the rest", disk["system_reserve_gb"] == 12 and disk["resources"]["mid"]["cmd"] == cfg["resources"]["mid"]["cmd"]
                  and list(disk["resources"]) == list(cfg["resources"]))
            st, a = await post({"system_reserve_gb": 1})
            check("too small a reserve is refused, file untouched", st == 400 and json.loads(path.read_text())["system_reserve_gb"] == 12, str(a)[:90])
            st, a = await post({"resources": {"mid": {"memory_gb": "lots"}}})
            check("bad value is refused", st == 400 and (await status())["resources"]["mid"]["memory_gb"] == 2)
            async with s.post(B + "/v1/chat/completions", headers=H, json={"model": "mid"}) as r: pass
            st, a = await post({"resources": {"small": {"memory_gb": 9999}}})
            async with s.post(B + "/v1/chat/completions", headers=H, json={"model": "small"}) as r: code = r.status
            j = await status()
            check("a model that asks for more than there is still loads, and alone", st == 200 and code == 200 and j["resources"]["small"]["whole"]
                  and j["resources"]["small"]["state"] == "ready" and j["resources"]["mid"]["state"] == "off", str({n: x["state"] for n, x in j["resources"].items()}))
            async with s.get(B + "/health") as r: check("this Mac is told where the key is", (await r.json()).get("key_file", "").endswith("key.txt"))
            async with s.post(B + "/scheduler/mint", headers=H, json={"name": "laptop"}) as r: tk = await r.json()
            T = {"Authorization": "Bearer " + tk.get("token", "x")}
            check("a token is made", tk.get("token", "").startswith("pc-") and tk["token"] not in (path.parent / "tokens.json").read_text())
            async with s.post(B + "/v1/chat/completions", headers=T, json={"model": "small"}) as r: check("the token can use a model", r.status == 200, str(r.status))
            async with s.get(B + "/scheduler/status", headers=T) as r: check("the token can read status, not as admin", r.status == 200 and (await r.json())["admin"] is False)
            async with s.post(B + "/scheduler/config", headers=T, json={"patch": {"system_reserve_gb": 20}}) as r: check("the token cannot change settings", r.status == 403)
            async with s.post(B + "/scheduler/mint", headers=T, json={"name": "x"}) as r: check("the token cannot make tokens", r.status == 403)
            async with s.get(B + "/scheduler/tokens", headers=H) as r: ls = (await r.json())["tokens"]
            check("tokens are listed with last use", len(ls) == 1 and ls[0]["name"] == "laptop" and ls[0]["last_used_ago_s"] is not None, str(ls))
            async with s.post(B + "/scheduler/revoke", headers=H, json={"id": tk["id"]}) as r: pass
            async with s.get(B + "/scheduler/status", headers=T) as r: check("a revoked token is refused", r.status == 401)
            async with s.get(B + "/scheduler/status", headers={"Authorization": "Bearer pc-nope"}) as r: check("an unknown token is refused", r.status == 401)
            async with s.get(B + "/props") as r: check("nothing behind the scheduler is reachable without a key", r.status == 401, str(r.status))
            async with s.get(B + "/scheduler/unload", headers=H) as r: check("a change needs POST", r.status == 405)
            async with s.post(B + "/scheduler/acquire", headers=H, json={"resource": "svc", "ttl_s": "soon"}) as r: a1 = r.status
            async with s.post(B + "/scheduler/acquire", headers=H, json={"resource": "svc", "ttl_s": 1e12}) as r: a2 = r.status
            async with s.post(B + "/scheduler/acquire", headers=H, data="[1]", ) as r: a3 = r.status
            check("a lease with a bad lifetime is refused and nothing stays held", (a1, a2, a3) == (400, 400, 400) and (await status())["resources"]["svc"]["active"] == 0, str((a1, a2, a3)))
            st, a = await post({"resources": {"mid": {"keep_alive_s": "300"}}})
            check("a time that is not a number is refused", st == 400, str(a)[:80])
            async with s.post(B + "/scheduler/announce", headers=H, json={"name": "Media MCP", "url": "https://example.test/mcp"}) as r: pass
            ep = (await status())["endpoints"]
            check("endpoints list the models and what was announced", ep[0]["url"] == B + "/v1" and {"name": "Media MCP", "url": "https://example.test/mcp"} in ep, str(ep))
            (path.parent / ".env").write_text("# mine\nKEEP=1\nOLD=x\n")
            async with s.post(B + "/scheduler/config", headers=H, json={"env": {"MODELS": "/m", "OLD": None}, "patch": {"public_url": "http://box:1", "listen": {"port": 18080}}}) as r: st = r.status
            async with s.get(B + "/scheduler/config", headers=H) as r: g = await r.json()
            check("variables are saved to .env, the rest of it stays", st == 200 and g["env"] == [["KEEP", "1"], ["MODELS", "/m"]] and "# mine" in (path.parent / ".env").read_text(), str(g["env"]))
            check("a partly given listen keeps working", (await status())["endpoints"][0]["url"] == "http://box:1/v1")
            st, a = await post({"listen": {"port": "8080"}})
            check("a port that is not a number is refused", st == 400)
            if not Path("/usr/local/sbin/pinecone-gpu-limit").exists():
                st, a = await post({"manage_gpu_limit": None}); await asyncio.sleep(1.5); j = await status()
                check("missing helper is reported, not fatal", st == 200 and j["gpu_limit"]["managed"] and "install-gpu-limit.sh" in (j["gpu_limit"]["error"] or ""), str(j["gpu_limit"]))
    finally:
        p.terminate(); p.wait()
    print("ALL PASS" if ok else "SOME FAILED"); sys.exit(0 if ok else 1)
asyncio.run(main())
