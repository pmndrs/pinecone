"""keep_cache against a real llama.cpp server: a model that is stopped to make room and started again reads on where it
was. Not part of the ordinary tests: it needs a llama-server binary and a model file (tiny_model.py makes one in a second).

    python cache_live.py <llama-server> <model.gguf>
"""
import json, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path
HERE = Path(__file__).resolve().parent
server, model = sys.argv[1], sys.argv[2]
B = "http://127.0.0.1:18080"; H = {"content-type": "application/json", "Authorization": "Bearer test-key"}
ok = True
def check(label, cond, extra=""):
    global ok; ok &= bool(cond); print("PASS " if cond else "FAIL ", label, extra)
def chat(name, messages, slot=0):
    r = urllib.request.Request(B + "/v1/chat/completions", headers=H,
                               data=json.dumps({"model": name, "messages": messages, "max_tokens": 4, "temperature": 0, "id_slot": slot}).encode())
    with urllib.request.urlopen(r, timeout=180) as f: t = json.load(f).get("timings", {})
    return t.get("prompt_n", 0), t.get("cache_n", 0)   # read anew, kept from before
def main():
    home = Path(tempfile.mkdtemp()).resolve()
    (home / "key.txt").write_text("test-key")
    cmd = [server, "-m", model, "-ngl", "0", "-c", "16384", "-np", "2", "--host", "127.0.0.1", "--port", "$PORT"]
    res = lambda port, **more: {"type": "llm", "memory_gb": 60, "keep_alive_s": 2, "min_residency_s": 1, "port": port, "cmd": cmd, **more}
    (home / "config.json").write_text(json.dumps({"listen": {"host": "127.0.0.1", "port": 18080}, "api_key_file": str(home / "key.txt"), "memory_budget_gb": 100,
                                                  "resources": {"kept": res(18112, keep_cache=True), "plain": res(18113)}}))
    p = subprocess.Popen([sys.executable, str(HERE.parent / "scheduler.py"), str(home / "config.json")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(B + "/health", timeout=1); break
            except OSError: time.sleep(0.2)
        long = {"role": "system", "content": "word " * 3000}
        a = [long, {"role": "user", "content": "first conversation says hello " * 20}]
        b = [long, {"role": "user", "content": "another conversation, in the other slot " * 20}]
        more = lambda m, i: m + [{"role": "assistant", "content": "ok"}, {"role": "user", "content": f"message {i}"}]
        for name, other in (("kept", "plain"), ("plain", "kept")):
            read, _ = chat(name, a, 0); chat(name, b, 1)
            check(f"{name}: a conversation is read once", read > 3000, str(read))
            read, kept = chat(name, more(a, 1), 0)
            check(f"{name}: and read on from there", read < 100 and kept > 3000, f"read {read}, kept {kept}")
            time.sleep(3); chat(other, [{"role": "user", "content": "hi"}]); time.sleep(3)   # does not fit next to it: it is stopped
            ra, ka = chat(name, more(more(a, 1), 2), 0); rb, kb = chat(name, more(b, 1), 1)
            if name == "kept":
                check("keep_cache: after the reload both conversations are read on where they were", ra < 100 and ka > 3000 and rb < 100 and kb > 3000, f"read {ra} and {rb}, kept {ka} and {kb}")
                check("keep_cache: the files are used up", not any((home / "cache" / "kept").iterdir()))
            else:
                check("without it: after the reload they are read from their first word", ra > 3000 and ka == 0, f"read {ra}, kept {ka}")
            time.sleep(3)
    finally:
        p.terminate(); p.wait()
    print("ALL PASS" if ok else "SOME FAILED")
    sys.exit(0 if ok else 1)
main()
