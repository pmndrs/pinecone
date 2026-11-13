"""Call one tool on the media MCP server: call.py <tool> '<json args>'  (or: call.py --list)"""
import json, sys, time, httpx
URL = __import__("os").environ.get("MEDIA_URL", "http://127.0.0.1:8090/mcp")  # set MEDIA_URL to go through a proxy
KEY = (__import__("pathlib").Path(__import__("os").environ.get("PINECONE_HOME", "~/.pinecone")).expanduser() / "internal-key.txt").read_text().strip()
H = {"Authorization": f"Bearer {KEY}", "Accept": "application/json, text/event-stream", "Content-Type": "application/json", "MCP-Protocol-Version": "2025-06-18"}
def rpc(method, params=None):
    with httpx.Client(timeout=None) as c:
        with c.stream("POST", URL, headers=H, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}) as r:
            if r.status_code != 200: r.read(); return {"http": r.status_code, "body": r.text[:500]}
            if "event-stream" in r.headers.get("content-type", ""):
                for line in r.iter_lines():
                    if line.startswith("data:"):
                        j = json.loads(line[5:])
                        if j.get("id") == 1: return j
                        print("  note:", json.dumps(j.get("params", j))[:160], file=sys.stderr)
            else: r.read(); return r.json()
if sys.argv[1] == "--list":
    j = rpc("tools/list"); print(json.dumps(j)[:300]) if "result" not in j else [print(t["name"], "-", t["description"].splitlines()[0], "|", list(t["inputSchema"].get("properties", {}))) for t in j["result"]["tools"]]
else:
    t0 = time.time(); j = rpc("tools/call", {"name": sys.argv[1], "arguments": json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}, "_meta": {"progressToken": "p1"}})
    res = j.get("result", j)
    for c in res.get("content", []) if isinstance(res, dict) else []:
        print(c["type"], (c.get("text") or f"<{c.get('mimeType')} {len(c.get('data',''))} b64 chars>"))
    if not (isinstance(res, dict) and res.get("content")): print(json.dumps(j)[:800])
    print(f"isError={res.get('isError') if isinstance(res, dict) else None}  took {time.time()-t0:.1f}s")
