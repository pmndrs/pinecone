"""A stand-in model server for scheduler tests."""
import asyncio, sys
from aiohttp import web
name, port = sys.argv[1], int(sys.argv[2])
async def health(r): return web.json_response({"status": "ok"})
async def chat(r):
    j = await r.json()
    if j.get("stream"):
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"}); await resp.prepare(r)
        for i in range(3):
            await resp.write(f'data: {{"from":"{name}","i":{i}}}\n\n'.encode()); await asyncio.sleep(float(j.get("delay", 0.2)))
        await resp.write(b"data: [DONE]\n\n"); return resp
    await asyncio.sleep(float(j.get("delay", 0)))
    return web.json_response({"from": name})
app = web.Application(); app.router.add_get("/health", health); app.router.add_post("/v1/chat/completions", chat)
web.run_app(app, host="127.0.0.1", port=port, print=None)
