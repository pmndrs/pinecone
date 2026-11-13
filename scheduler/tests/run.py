"""Main test: loading on demand, eviction, leases, queueing, starvation, crashes. Needs a scheduler that was started
with tests/config.json (port 18080; the stand-in servers use 18101 to 18105)."""
import asyncio, sys, time
import aiohttp
B="http://127.0.0.1:18080"; H={"Authorization":"Bearer test-key"}
ok=True
def check(label, cond, extra=""):
    global ok; ok &= bool(cond); print(("PASS " if cond else "FAIL "), label, extra)
async def states(s):
    async with s.get(B+"/scheduler/status",headers=H) as r: j=await r.json()
    return {n:x["state"] for n,x in j["resources"].items()}
async def chat(s, model, stream=False, delay=0):
    t=time.time()
    async with s.post(B+"/v1/chat/completions",headers=H,json={"model":model,"stream":stream,"delay":delay}) as r:
        return r.status, await r.text(), time.time()-t
async def main():
    async with aiohttp.ClientSession() as s:
        async with s.get(B+"/v1/models") as r: check("no key is refused", r.status==401)
        async with s.get(B+"/health") as r: check("health is open", r.status==200)
        async with s.get(B+"/v1/models",headers=H) as r: check("models lists only llms", [m["id"] for m in (await r.json())["data"]]==["big","mid","small","broken"])
        st,tx,_=await chat(s,"nope"); check("unknown model is 404", st==404)
        st,tx,_=await chat(s,"broken"); check("failed start reports 503", st==503, tx[:80])
        st,tx,_=await chat(s,"mid"); check("mid loads on demand", st==200 and '"mid"' in tx)
        async with s.post(B+"/scheduler/acquire",headers=H,json={"resource":"svc","ttl_s":30}) as r: lease=(await r.json())
        check("svc lease granted next to mid", (await states(s))["svc"]=="ready" and (await states(s))["mid"]=="ready", str(lease.get("url")))
        # small does not fit next to mid+svc (140>100): must evict LRU idle one (mid, idle), svc is busy
        st,tx,dt=await chat(s,"small"); stt=await states(s)
        check("small evicts idle mid, keeps busy svc", st==200 and stt["mid"]=="off" and stt["svc"]=="ready" and stt["small"]=="ready", f"{dt:.1f}s")
        # big needs the whole budget: must wait for the svc lease to be released
        big=asyncio.ensure_future(chat(s,"big",stream=True))
        await asyncio.sleep(3); check("big waits while svc is leased", not big.done() and (await states(s))["big"]=="off")
        async with s.post(B+"/scheduler/release",headers=H,json={"lease":lease["lease"]}) as r: pass
        st,tx,dt=await big; stt=await states(s)
        check("big ran alone after waiting", st==200 and '"big"' in tx and stt["small"]=="off" and stt["svc"]=="off", f"{dt:.1f}s")
        check("queued stream got keep-alive comments", ": waiting for big" in tx)
        # mid must wait for big's keep-alive (5s)
        st,tx,dt=await chat(s,"mid"); stt=await states(s)
        check("mid waited for big's keep-alive", st==200 and dt>=4 and stt["big"]=="off", f"{dt:.1f}s")
        # starvation: keep mid busy with back-to-back requests; big should get in after ~12s
        stop=False
        async def hammer():
            while not stop: await chat(s,"mid",delay=0.5)
        hs=[asyncio.ensure_future(hammer()) for _ in range(2)]
        await asyncio.sleep(1); st,tx,dt=await chat(s,"big"); stop=True
        check("starving big request gets in despite busy mid", st==200 and 11<dt<25, f"{dt:.1f}s")
        await asyncio.gather(*hs)
        # a caller that goes away leaves no waiter behind
        t=asyncio.ensure_future(chat(s,"small")); await asyncio.sleep(1); t.cancel()
        await asyncio.sleep(3)
        async with s.get(B+"/scheduler/status",headers=H) as r: j=await r.json()
        check("cancelled caller left the queue", not any(q["resource"]=="small" for q in j["queue"]), str(j["queue"]))
        # idle stop for svc
        async with s.post(B+"/scheduler/acquire",headers=H,json={"resource":"svc","ttl_s":2}) as r: lease=await r.json()
        await asyncio.sleep(4); 
        async with s.get(B+"/scheduler/status",headers=H) as r: j=await r.json()
        check("unrenewed lease expires", not j["leases"])
        await asyncio.sleep(8); check("idle service stops itself", (await states(s))["svc"]=="off")
    print("ALL PASS" if ok else "SOME FAILED")
    sys.exit(0 if ok else 1)
asyncio.run(main())
