// The reel, as a function of time: renderAt(seconds) draws one frame. Nothing here runs on a clock, so the
// film can be drawn frame by frame, in any order. The first half is the machinery, the second the story.
"use strict";
const $ = (id) => document.getElementById(id);
const clamp = (v, a = 0, b = 1) => Math.max(a, Math.min(b, v));
const ease = (p) => (p < 0.5 ? 4 * p * p * p : 1 - Math.pow(-2 * p + 2, 3) / 2);
const lerp = (a, b, p) => a + (b - a) * p;
const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;");
const gb = (v) => (v >= 10 ? `${Math.round(v)} GB` : `${v.toFixed(1)} GB`);

// ---- what Pinecone manages in this film ----
const TOTAL = 256, RESERVE = 8, SYSTEM = 21;
const PALETTE = ["#0a84ff", "#bf5af2", "#40c8e0", "#ff375f", "#5e5ce6", "#66d4cf"];
const RES = {
	deepseek: { title: "DeepSeek V3 671B, 2-bit", kind: "llm", need: 256, size: 226, load: 3.2 },
	gemma: { title: "Gemma 3 27B Q8", kind: "llm", need: 30, size: 27, load: 1.6 },
	qwen: { title: "Qwen3 Coder 30B Q6", kind: "llm", need: 32, size: 29, load: 1.6 },
	audio: { title: "mlx-audio (speech, transcription)", kind: "service", need: 10, size: 4, load: 1.0 },
	comfy: { title: "ComfyUI (images, music, sound)", kind: "service", need: 40, size: 18, load: 1.8 },
	video: { title: "Wan 2.2 (video)", kind: "service", need: 190, size: 148, load: 2.6 },
};
Object.keys(RES).forEach((n, i) => (RES[n].color = PALETTE[i]));
const UNLOAD = 0.7;

// ---- the timeline: things that happen at a time, and the state they add up to ----
const EV = [], BLOCKS = [], SFX = [], MARKS = [], CAPS = [], SHOTS = [], TYPING = [];
let T = 0;
const at = (t) => (T = t), wait = (d) => (T += d), mark = (name) => MARKS.push([T, name]);
const on = (fn, dt = 0) => EV.push({ t: T + dt, fn });
const note = (text, dt = 0) => on((st, t) => st.log.unshift({ t, text }), dt);

function stateAt(time) {
	const st = { res: {}, log: [], queue: [], panel: null, model: "gemma" };
	for (const n in RES) st.res[n] = { state: "off", since: -99, active: 0 };
	for (const e of EV) if (e.t <= time) e.fn(st, e.t);
	return st;
}
const set = (name, state) => (st, t) => Object.assign(st.res[name], { state, since: t });
function actual(name, r, time) {
	const d = RES[name], p = time - r.since;
	if (r.state === "starting") return d.size * ease(clamp(p / d.load));
	if (r.state === "stopping") return d.size * (1 - ease(clamp(p / UNLOAD)));
	return r.state === "ready" ? d.size : 0;
}
// The story's verbs. Each puts its events at the current time and moves it on by `dt` seconds.
function load(name, dt = 0) {
	on(set(name, "starting"), dt); note(`starting ${name}`, dt);
	on(set(name, "ready"), dt + RES[name].load); note(`${name} ready after ${Math.round(RES[name].load * 4)}s`, dt + RES[name].load);
	return dt + RES[name].load;
}
function unload(name, why, dt = 0) {
	on(set(name, "stopping"), dt); note(`stopping ${name}${why ? ` to make room for ${why}` : ": idle"}`, dt);
	on(set(name, "off"), dt + UNLOAD); note(`${name} stopped`, dt + UNLOAD);
	return dt + UNLOAD;
}
const work = (name, by, dt = 0) => on((st, t) => { st.res[name].active += by; st.res[name].since = t; }, dt);
const queue = (name, why, from, to) => { on((st) => st.queue.push({ name, why, from: T + from }), from); on((st) => (st.queue = st.queue.filter((q) => q.name !== name)), to); };

// ---- the terminal's side: blocks appear in order, the user's lines are typed first ----
const block = (b) => (BLOCKS.push({ t0: T, ...b }), b);
function say(text, cps = 19) {
	const t0 = T;
	[...text].forEach((ch, i) => SFX.push({ t: t0 + i / cps + 0.02 * Math.sin(i * 7.3), k: ch === " " ? "key-space" : "key" }));
	T += text.length / cps + 0.25;
	TYPING.push({ t0, t1: T, text, cps });
	SFX.push({ t: T, k: "enter" });
	block({ kind: "user", text });
	wait(0.35);
}
const tool = (name, dur, arg = "") => block({ kind: "tool", name, arg, t1: T + dur });
const answer = (text, cps = 70, cls = "") => { const b = block({ kind: "text", text, cps, cls }); wait(text.length / cps); return b; };
const caption = (s, dur, dt = 0) => CAPS.push({ from: T + dt, to: T + dt + dur, s });
const shot = (z, cx, cy, dur = 1.1, dt = 0) => SHOTS.push({ t0: T + dt, t1: T + dt + dur, z, cx, cy });
const HOME = [1, 960, 540];

// ---- drawing ----
const MARK = (fill, dot) => `<svg id="mark" viewBox="0 0 800 800"><g fill="${fill}"><rect x="280" y="560" width="240" height="240"/><rect x="280" y="280" width="240" height="240"/><rect x="0" y="280" width="240" height="240"/><path d="M560 0H280V240H560V520H800V0H560Z"/>${dot ? '<circle cx="120" cy="120" r="95"/>' : ""}</g></svg>`;
const SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏";
const noise = (t, k) => Math.sin(t * 2.1 + k) * 0.5 + Math.sin(t * 5.3 + k * 2.7) * 0.3 + Math.sin(t * 11.7 + k * 1.3) * 0.2;

function numbers(st, time) {
	let ours = 0, busy = 0, gpu = 0, moving = 0;
	for (const n in st.res) {
		const r = st.res[n];
		ours += actual(n, r, time);
		if (r.active > 0) { busy++; gpu = Math.max(gpu, RES[n].kind === "llm" ? 92 : 97); }
		if (r.state === "starting" || r.state === "stopping") moving++;
	}
	return { ours, mem: SYSTEM + ours + 1.2 * noise(time * 0.3, 1),
		cpu: clamp(4 + 26 * moving + 9 * busy + 3 * noise(time, 2), 1, 99), gpu: clamp(gpu ? gpu + 4 * noise(time, 3) : 3 + 28 * moving + 2 * noise(time, 4), 0, 100) };
}

function spark(values, top, color) {
	const pts = values.map((v, i) => `${(i / (values.length - 1) * 100).toFixed(1)},${(33 - clamp(v / top) * 31).toFixed(1)}`);
	return `<svg viewBox="0 0 100 34" preserveAspectRatio="none"><polygon points="0,34 ${pts.join(" ")} 100,34" fill="${color}" opacity=".18"/><polyline points="${pts.join(" ")}" fill="none" stroke="${color}" stroke-width="1.5" vector-effect="non-scaling-stroke"/></svg>`;
}

function panelHTML(st, time) {
	const now = numbers(st, time), names = Object.keys(RES);
	const working = names.some((n) => st.res[n].active > 0 || ["starting", "stopping"].includes(st.res[n].state));
	const loaded = names.filter((n) => st.res[n].state !== "off");
	const slices = loaded.map((n) => ({ n, v: actual(n, st.res[n], time), c: RES[n].color })).filter((s) => s.v > 0.3);
	const free = TOTAL - SYSTEM - now.ours;
	const bar = slices.map((s) => `<i style="width:${(s.v / TOTAL * 100).toFixed(2)}%;background:${s.c}"></i>`).join("") + `<i style="width:${(SYSTEM / TOTAL * 100).toFixed(2)}%;background:rgba(235,235,245,.45)"></i><span class="keep" style="width:${RESERVE / TOTAL * 100}%"></span>`;
	const legend = slices.map((s) => `<span><i style="background:${s.c}"></i>${s.n} ${gb(s.v)}</span>`).join("") + `<span><i style="background:rgba(235,235,245,.45)"></i>system and apps ${gb(SYSTEM)}</span><span class="free">${gb(free)} free</span>`;
	const row = (n) => {
		const r = st.res[n], d = RES[n], v = actual(n, r, time);
		const dot = r.state === "off" ? "var(--off)" : r.state === "ready" && !r.active ? "var(--ok)" : "var(--busy)";
		const detail = r.state === "starting" ? "loading…" : r.state === "stopping" ? "unloading…" : r.state === "off" ? (d.need >= TOTAL - RESERVE ? "needs the whole Mac" : `needs ${gb(d.need)}`)
			: `${gb(v)} · ${r.active ? "working" + (r.active > 1 ? ` on ${r.active}` : "") : `idle ${Math.max(0, Math.floor(time - r.since))} s`}`;
		return `<div class="res ${r.state === "off" ? "off" : ""}"><i class="dot" style="background:${dot}"></i><div class="nm"><b>${n}</b><small>${esc(d.title)}</small></div>${r.state !== "off" ? `<i class="tint" style="background:${d.color}"></i>` : ""}<span class="dt">${detail}</span>${r.state === "ready" && !r.active ? '<span class="ej">⏏</span>' : ""}</div>`;
	};
	const sect = (title, kind) => { const list = names.filter((n) => RES[n].kind === kind); return `<div class="sec"><h6>${title}<em>${list.filter((n) => st.res[n].state !== "off").length} of ${list.length} loaded</em></h6>${list.map(row).join("")}</div>`; };
	const series = Array.from({ length: 60 }, (_, i) => { const tt = time - (59 - i) * 0.25; return numbers(stateAt(tt), tt); });
	const tile = (title, key, top, unit, color) => `<div><small>${title}</small><b>${Math.round(now[key])}<i>${unit}</i></b>${spark(series.map((s) => s[key]), top, color)}</div>`;
	const waiting = st.queue.length ? `<div class="sec wait"><h6>Waiting</h6>${st.queue.map((q) => `<div>${q.name}: ${Math.max(0, Math.floor(time - q.from))} s<small>${esc(q.why)}</small></div>`).join("")}</div>` : "";
	return `<div class="hd"><i class="dot" style="background:${working ? "var(--busy)" : "var(--ok)"}"></i><b>Pinecone</b><span>${working ? "Working" : "On"}</span><i class="sw"></i></div>
		<div class="mem">${bar}</div><div class="lg">${legend}</div>${sect("Models", "llm")}${sect("Services", "service")}${waiting}
		<div class="gr">${tile("CPU", "cpu", 100, "%", "#0a84ff")}${tile("GPU", "gpu", 100, "%", "#bf5af2")}${tile("Memory", "mem", TOTAL, `of ${TOTAL} GB`, "#40c8e0")}</div>
		<div class="sec"><h6>Endpoints<em>an access token is the key</em></h6>
			<div class="ep"><div><small>Models (OpenAI-compatible)</small><code>http://studio.local:8080/v1</code></div><span class="btn">Copy</span></div>
			<div class="ep"><div><small>Media tools (MCP)</small><code>https://studio.local:8443/mcp</code></div><span class="btn">Copy</span></div></div>
		<div class="act"><h6>Recent activity</h6>${st.log.slice(0, 4).map((e) => `<div><span>${Math.max(0, Math.floor(time - e.t))} s</span>${esc(e.text)}</div>`).join("") || "<div><span></span>nothing yet</div>"}</div>`;
}

function blockHTML(b, time) {
	const p = time - b.t0;
	if (b.kind === "user") return `<div class="user">${esc(b.text)}</div>`;
	if (b.kind === "tool") {
		const done = time >= b.t1, secs = (Math.min(time, b.t1) - b.t0) * 4;
		return `<div>${done ? '<span class="g">✓</span>' : `<span class="am">${SPIN[Math.floor(time * 12) % 10]}</span>`} <span class="${done ? "" : "am"}">${esc(b.name)}</span> <span class="d">${esc(b.arg)}${b.arg ? " · " : ""}${secs.toFixed(1)} s</span></div>`;
	}
	if (b.kind === "text") return `<div class="${b.cls}">${esc(b.text.slice(0, Math.floor(p * b.cps)))}</div>`;
	if (b.kind === "pic") return `<div style="opacity:${ease(clamp(p / 0.4))}"><img class="pic" src="assets/cone.jpg"></div>`;
	if (b.kind === "clip") {
		const q = clamp(p / b.dur), z = 1 + 0.16 * q;
		return `<div style="opacity:${ease(clamp(p / 0.4))}"><div class="clip"><img src="assets/cone.jpg" style="transform:scale(${z})"><img src="assets/cone-snow.jpg" style="transform:scale(${z});opacity:${ease(clamp((q - 0.15) / 0.5))}"><div class="bar2"><i style="width:${q * 100}%"></i></div></div></div>`;
	}
	if (b.kind === "voice") {
		const playing = p >= 0 && p < b.dur, s = Math.floor(clamp(p, 0, b.dur));
		const bars = Array.from({ length: 26 }, (_, i) => `<i style="height:${playing ? 5 + 20 * Math.abs(noise(time * 6 + i * 0.9, i)) : 4}px;opacity:${i / 26 < p / b.dur ? 1 : 0.4}"></i>`).join("");
		return `<div><span class="chip"><span class="a">${playing ? "❚❚" : "▶"}</span><span class="wave">${bars}</span><span>♪ ${esc(b.title)}</span><span class="d">0:${String(s).padStart(2, "0")} / 0:${String(Math.round(b.dur)).padStart(2, "0")}</span></span></div>`;
	}
	if (b.kind === "team") return `<div>${b.rows.map((r) => {
		const from = b.t0 + r.from, to = b.t0 + r.to;
		const st = time < from ? `<span class="d">·</span>` : time < to ? `<span class="am">${SPIN[Math.floor(time * 12) % 10]}</span>` : `<span class="g">✓</span>`;
		return `<div class="row">${st}<span class="n c">${r.who}</span><span class="w d">on ${r.model}</span><span class="${time < to ? "d" : ""}">${time < from ? "waiting for memory" : time < to ? r.doing : r.done}</span></div>`;
	}).join("")}</div>`;
	return "";
}

function cameraAt(time) {
	let cur = HOME;
	for (const s of SHOTS) {
		if (time >= s.t1) cur = [s.z, s.cx, s.cy];
		else if (time > s.t0) { const p = ease((time - s.t0) / (s.t1 - s.t0)); return [lerp(cur[0], s.z, p), lerp(cur[1], s.cx, p), lerp(cur[2], s.cy, p)]; }
	}
	return cur;
}
const fade = (time, from, to, ramp = 0.3) => Math.min(clamp((time - from) / ramp), clamp((to - time) / ramp));

let SPEED = 1;
function renderAt(seconds) {
	const time = seconds * SPEED, st = stateAt(time);
	let [z, cx, cy] = cameraAt(time);
	cx = clamp(cx, 960 / z, 1920 - 960 / z); cy = clamp(cy, 540 / z, 1080 - 540 / z);  // never look past the edge of the desk
	$("world").style.transform = `translate(${960 - cx * z}px, ${540 - cy * z}px) scale(${z})`;
	// the menu bar icon and the panel under it
	const working = Object.values(st.res).some((r) => r.active > 0 || r.state === "starting" || r.state === "stopping");
	$("markbox").innerHTML = MARK("#f2f2f5", working);
	const open = st.panel === null ? 0 : ease(clamp((time - st.panel) / 0.32));
	$("panel").style.display = open ? "block" : "none";
	$("panel").style.opacity = open;
	$("panel").style.transform = `scale(${0.94 + 0.06 * open}) translateY(${(1 - open) * -10}px)`;
	if (open) $("panel").innerHTML = panelHTML(st, time);
	// the terminal
	$("talk").innerHTML = BLOCKS.filter((b) => b.t0 <= time).map((b) => blockHTML(b, time)).join("");
	const typing = TYPING.find((x) => time >= x.t0 && time < x.t1);
	const caret = Math.floor(time * 2) % 2 === 0 || typing ? '<span class="caret"></span>' : "";
	$("prompt").innerHTML = `<span style="color:var(--accent)">❯</span> ${typing ? esc(typing.text.slice(0, Math.floor((time - typing.t0) * typing.cps) + 1)) : ""}${caret}`;
	$("footl").textContent = `local/${st.model}`;
	// captions, pointer, cards
	const cap = CAPS.find((c) => time >= c.from && time < c.to);
	$("caption").innerHTML = cap ? `<span style="opacity:${fade(time, cap.from, cap.to, 0.25)}">${esc(cap.s)}</span>` : "";
	const ptr = pointerAt(time);
	$("cursor").style.display = ptr ? "block" : "none";
	if (ptr) $("cursor").style.transform = `translate(${ptr.x}px, ${ptr.y}px)`;
	const sinceClick = time - CLICK.t;
	$("ring").style.display = sinceClick >= 0 && sinceClick < 0.4 ? "block" : "none";
	$("ring").style.transform = `translate(${CLICK.x}px, ${CLICK.y}px) scale(${0.3 + sinceClick * 2.2})`;
	$("ring").style.opacity = 1 - sinceClick / 0.4;
	$("open").style.opacity = 1 - ease(clamp((time - OPEN_END) / 0.7));
	$("open").style.display = time < OPEN_END + 0.7 ? "flex" : "none";
	$("close").style.opacity = ease(clamp((time - CLOSE_AT) / 0.8));
	$("close").style.display = time >= CLOSE_AT ? "flex" : "none";
}

// ============================== the story ==============================
const OPEN_END = 2.9, ICON = { x: 1619, y: 18 }, CLICK = { t: 4.5, x: ICON.x, y: ICON.y };
const pointerAt = (time) => {
	if (time < 3.5 || time > 6.2) return null;
	const p = ease(clamp((time - 3.6) / 0.85));
	return { x: lerp(1380, ICON.x - 4, p), y: lerp(420, ICON.y - 2, p) };
};

mark("title");
at(CLICK.t); SFX.push({ t: T, k: "click" }); on((st, t) => (st.panel = t));
caption("Pinecone lives in the menu bar. Everything is off, the memory is free.", 3.4, 0.5);
shot(1.32, 1560, 400, 1.2, 0.4); shot(...HOME, 1.1, 3.6);

// 1: a question loads a small model
at(9.3); mark("a question");
say("what is a pine cone?");
caption("A request loads its model.", 3.6);
let d = load("gemma"); work("gemma", +1, d); wait(d + 0.4);
answer("A pine cone is how a pine tree keeps its seeds safe. Its scales open\nwhen the air is dry, and close again when it rains.");
answer("gemma · 41 t/s", 200, "d"); work("gemma", -1); wait(0.9);

// 2: speech is a service, reached as an MCP tool
mark("speech");
say("say that out loud, in a warm voice");
caption("Speech, pictures and music are services. Pinecone serves them as MCP tools.", 4.6);
tool("mcp__media__speak", 2.6, "Serena");
d = load("audio"); work("audio", +1, d); work("audio", -1, 2.6); wait(2.7);
block({ kind: "voice", title: "What a pine cone is", dur: 9.7 }); SFX.push({ t: T + 0.15, k: "voice", f: "assets/speech.wav", gain: 0.9 });
wait(2.4);

// 3: a picture, next to what is already loaded
mark("picture");
say("now make a picture of one");
caption("Side by side, as long as they fit into what you left them.", 4.6, 0.6);
tool("mcp__media__generate_image", 4.4, "1024 × 1024");
d = load("comfy"); work("comfy", +1, d); work("comfy", -1, 4.4); wait(4.5);
block({ kind: "pic" }); wait(2.6);

// 4: video needs more room than is free
mark("video");
say("turn it into a short video");
caption("Not enough room? Whatever is idle makes way.", 4.4, 0.4);
tool("mcp__media__generate_video", 7.0, "3 s, 720p");
queue("video", "waiting for: gemma is unloading", 0.1, UNLOAD + 0.2);
d = unload("gemma", "video", 0.2); d = load("video", d + 0.1); work("video", +1, d); work("video", -1, 7.0); wait(7.1);
block({ kind: "clip", dur: 4.2 }); wait(4.6);

// 5: the big model takes the whole Mac
mark("the big model");
say("/model deepseek"); on((st) => (st.model = "deepseek")); answer("model: deepseek (needs the whole Mac)", 200, "d"); wait(0.3);
say("plan a field guide to pine cones, then write it");
caption("The big model needs the whole Mac, so it gets it.", 4.8, 0.5);
shot(1.3, 1560, 300, 1.1, 0.3); shot(...HOME, 1.1, 4.6);
queue("deepseek", "waiting for: video, comfy, audio are unloading", 0.1, UNLOAD + 0.3);
unload("video", "deepseek", 0.15); unload("comfy", "deepseek", 0.25); unload("audio", "deepseek", 0.35);
d = load("deepseek", UNLOAD + 0.5); work("deepseek", +1, d); wait(d + 0.5);
answer("Plan: 1 research twelve species  2 write a page for each\n      3 paint a picture for each  4 put the guide together\nI'll hand 1 to 3 to smaller models and take 4 myself.");
work("deepseek", -1); wait(0.8);

// 6: big and small models take turns
mark("taking turns");
caption("Big and small models take turns. Nobody starts or stops a server by hand.", 6.5, 0.6);
const team = block({ kind: "team", rows: [
	{ who: "researcher", model: "gemma", from: 2.6, to: 6.8, doing: "reading about twelve species…", done: "12 species, 31 sources" },
	{ who: "writer", model: "qwen", from: 2.8, to: 7.9, doing: "writing the pages…", done: "12 pages" },
	{ who: "illustrator", model: "comfy", from: 3.0, to: 8.6, doing: "painting…", done: "12 pictures" } ] });
queue("gemma", "waiting for: deepseek is unloading", 0.3, 1.2); queue("qwen", "waiting for: deepseek is unloading", 0.4, 1.2);
unload("deepseek", "gemma", 0.4);
load("gemma", 1.0); load("qwen", 1.2); load("comfy", 1.2);
work("gemma", +1, 2.6); work("qwen", +1, 2.8); work("comfy", +1, 3.0);
work("gemma", -1, 6.8); work("qwen", -1, 7.9); work("comfy", -1, 8.6);
wait(8.9);
queue("deepseek", "waiting for: gemma, qwen, comfy are unloading", 0.1, UNLOAD + 0.4);
unload("gemma", "deepseek", 0.2); unload("qwen", "deepseek", 0.3); unload("comfy", "deepseek", 0.4);
d = load("deepseek", UNLOAD + 0.6); work("deepseek", +1, d); wait(d + 0.4);
answer("Done: field-guide.pdf, 12 pages with 12 pictures.");
answer("deepseek · 9 t/s", 200, "d"); work("deepseek", -1); wait(2.4);

mark("the end");
const CLOSE_AT = T; wait(5.2);
const DURATION = T;
EV.sort((a, b) => a.t - b.t);  // the story is not written in the order things happen
