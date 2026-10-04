"use strict";

const SVGNS = "http://www.w3.org/2000/svg";
const $ = (id) => document.getElementById(id);

const state = {
  station: null,          // /api/state snapshot
  yardText: null,         // pending import text
  sim: new Map(),         // route instance -> {stage, seq}
};

// ---------------------------------------------------------------------------
// tiny helpers
// ---------------------------------------------------------------------------

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: {"Content-Type": "application/json"}, ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.detail || res.statusText),
                                  {data});
  return data;
}

function el(tag, attrs = {}, children = []) {
  const node = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "text") node.textContent = v; else node.setAttribute(k, v);
  }
  for (const c of [].concat(children)) if (c) node.appendChild(c);
  return node;
}

function html(node, ...kids) {
  kids.forEach((k) => node.appendChild(
      k instanceof Node ? k : document.createTextNode(String(k))));
}
function make(tag, cls) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  return n;
}

// ---------------------------------------------------------------------------
// import
// ---------------------------------------------------------------------------

$("yard-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  state.yardText = await file.text();
  $("btn-import").disabled = false;
  showImport(`已选择 ${file.name}，点击“导入”`, "");
});

$("load-demo").addEventListener("click", async () => {
  const res = await fetch("/static/demo_yard.json");
  if (!res.ok) {
    showImport("未能自动取示例文件，请手动选择 examples/demo_yard.json",
               "err");
    return;
  }
  state.yardText = await res.text();
  $("btn-import").disabled = false;
  $("yard-file").value = "";
  showImport("已载入示例站场，点击“导入”", "");
});

$("btn-import").addEventListener("click", async () => {
  if (!state.yardText) return;
  try {
    const r = await api("/api/station", {
      method: "POST", body: state.yardText});
    showImport(`导入成功：${r.name}，${r.routes.length} 条进路（事件 #${r.event_id}）`, "ok");
  } catch (e) {
    showImport(`导入被拒绝：${e.message}` +
      (e.data && e.data.detail ? "" : ""), "err");
    console.error(e);
  }
  await refresh();
});

function showImport(text, kind) {
  const m = $("import-msg");
  m.textContent = text;
  m.className = "msg " + (kind || "");
}

// ---------------------------------------------------------------------------
// diagram
// ---------------------------------------------------------------------------

function nodePos(st, nodeId) {
  const ep = st.endpoints.find((e) => e.id === nodeId);
  if (ep && ep.x != null) return {x: ep.x, y: ep.y};
  const sw = st.switches.find((s) => s.id === nodeId);
  if (sw && sw.x != null) return {x: sw.x, y: sw.y};
  return null;
}

function sectionColor(secId, snap) {
  const active = snap.active_routes || [];
  const fault = active.some((r) => r.state === "FAULT" &&
      r.sections.some((s) => s.id === secId));
  if (fault) return "#b84bd8";
  const rs = active.flatMap((r) => r.sections.map((s) => ({r, s})))
      .find((x) => x.s.id === secId);
  if (rs) {
    if (rs.s.global_occupied) return "#e0604d";
    if (rs.s.cleared) return "#4a86f5";
    return "#e8c468"; // locked
  }
  return "#6b7891";
}

function drawDiagram() {
  const svg = $("diagram");
  svg.innerHTML = "";
  const snap = state.station;
  if (!snap) {
    svg.appendChild(el("text", {x: 320, y: 130, fill: "#5a6780",
      "text-anchor": "middle", "font-size": 15,
      text: "尚未导入站场"}));
    return;
  }
  const st = snap.station;

  // sections
  for (const sec of st.sections) {
    const a = nodePos(st, sec.a), b = nodePos(st, sec.b);
    if (!a || !b) continue;
    let midx = sec.x, midy = sec.y;
    if (midx == null) {
      midx = (a.x + b.x) / 2;
      midy = (a.y + b.y) / 2;
    }
    svg.appendChild(el("line", {
      x1: a.x, y1: a.y, x2: b.x, y2: b.y,
      stroke: "#2a3547", "stroke-width": 11, "stroke-linecap": "round"}));
    svg.appendChild(el("line", {
      x1: a.x, y1: a.y, x2: b.x, y2: b.y,
      stroke: sectionColor(sec.id, snap), "stroke-width": 7,
      "stroke-linecap": "round", class: "track"}));
    svg.appendChild(el("text", {
      x: midx, y: midy - 12, fill: "#9db1d1", "font-size": 10.5,
      "text-anchor": "middle", text: sec.id}));
    if (sec.detector) {
      svg.appendChild(el("circle", {cx: midx, cy: midy + 10, r: 3.2,
        fill: "#10141b", stroke: "#9db1d1", "stroke-width": 1}));
      svg.appendChild(el("text", {x: midx + 8, y: midy + 14,
        fill: "#6f7d97", "font-size": 9, text: sec.detector}));
    }
  }

  // switches
  for (const sw of st.switches) {
    const p = nodePos(st, sw.id);
    if (!p) continue;
    svg.appendChild(el("circle", {cx: p.x, cy: p.y, r: 5.5,
      fill: "#e8c468", stroke: "#0d1219", "stroke-width": 1.5}));
    svg.appendChild(el("text", {x: p.x, y: p.y - 12, fill: "#ffd98a",
      "font-size": 10.5, "text-anchor": "middle", text: sw.id}));
  }

  // endpoints + signals
  for (const ep of st.endpoints) {
    const p = nodePos(st, ep.id);
    if (!p) continue;
    const active = (snap.active_routes || []).some(
      (r) => r.state === "ACTIVE" &&
      (r.route_id.startsWith(ep.id + "-") ||
       r.route_id.endsWith("-" + ep.id)));
    svg.appendChild(el("rect", {x: p.x - 4, y: p.y - 4, width: 8,
      height: 8, fill: "#9db1d1"}));
    if (ep.signal) {
      svg.appendChild(el("circle", {cx: p.x + (ep.x < 300 ? -14 : 14),
        cy: p.y - 10, r: 4.5,
        fill: active ? "#3fd07a" : "#c8403a",
        stroke: "#0d1219", "stroke-width": 1}));
      svg.appendChild(el("text", {x: p.x, y: p.y + 22,
        fill: "#9db1d1", "font-size": 10.5, "text-anchor": "middle",
        text: `${ep.id}${ep.signal !== ep.id ? "/" + ep.signal : ""}`}));
    }
  }
}

// ---------------------------------------------------------------------------
// routes panel
// ---------------------------------------------------------------------------

function renderRoutes() {
  const box = $("route-list");
  box.innerHTML = "";
  const snap = state.station;
  if (!snap) {
    box.innerHTML = "<em>导入站场后显示进路</em>";
    return;
  }
  const blocked = snap.blocked;
  for (const rt of snap.station.routes) {
    const actives = snap.active_routes.filter(
      (r) => r.route_id === rt.id);
    const card = make("div", "route-card");
    const state0 = actives[0] && actives[0].state;
    if (state0 === "ACTIVE") card.classList.add("active");
    if (state0 === "FAULT") card.classList.add("fault");
    if (state0 === "COMPLETED") card.classList.add("completed");

    const head = make("div", "head");
    html(head, make("span", "rid"));
    head.querySelector(".rid").textContent = rt.id;
    if (state0) {
      const b = make("span", "badge " + state0.toLowerCase());
      b.textContent = {ACTIVE: "锁闭中", FAULT: "故障锁闭",
        COMPLETED: "已完成"}[state0] || state0;
      head.appendChild(b);
    }
    const btn = make("button");
    btn.textContent = "申请放行";
    if (blocked || state0 === "ACTIVE" || state0 === "FAULT")
      btn.disabled = true;
    btn.addEventListener("click", () => requestRoute(rt.id));
    const sp = make("span"); sp.className = "spacer"; head.appendChild(sp);
    head.appendChild(btn);
    card.appendChild(head);

    const meta = make("div", "meta");
    meta.textContent =
      `${rt.from} → ${rt.to}｜区段: ${rt.sections.join("、")}｜` +
      `道岔: ${rt.switches.map((s) => s.id + "@" + s.position).join("，") || "—"}｜` +
      `侧防: ${rt.flanks.join("、") || "—"}｜信号: ${rt.signal || "—"}`;
    card.appendChild(meta);
    box.appendChild(card);
  }
}

async function requestRoute(routeId) {
  const box = $("verdict-box");
  box.innerHTML = "<em>核对中…</em>";
  try {
    const v = await api("/api/routes/request", {
      method: "POST", body: JSON.stringify({route_id: routeId})});
    renderVerdict(v);
  } catch (e) {
    box.innerHTML = "";
    const d = make("div", "vdenied");
    d.textContent = "请求失败：" + e.message;
    box.appendChild(d);
  }
  await refresh();
}

function renderVerdict(v) {
  const box = $("verdict-box");
  box.innerHTML = "";
  const head = make("div");
  if (v.verdict === "GRANT") {
    head.className = "vgranted";
    head.textContent = `✔ 放行 ${v.route_id} — ${v.signal.display}` +
      `（实例 #${v.route_instance}，事件 #${v.event_id}）`;
  } else {
    head.className = "vdenied";
    head.textContent = `✘ 拒绝 ${v.route_id} — ${v.signal.display}` +
      `（事件 #${v.event_id}）`;
  }
  box.appendChild(head);
  const pre = make("pre");
  pre.textContent = JSON.stringify(v, null, 2);
  box.appendChild(pre);
}

// ---------------------------------------------------------------------------
// detection form + active route simulation
// ---------------------------------------------------------------------------

function renderDetectors() {
  const ds = $("det-section"), dd = $("det-detector");
  ds.innerHTML = ""; dd.innerHTML = "<option value=''>（按区段）</option>";
  if (!state.station) return;
  for (const sec of state.station.station.sections) {
    const o = make("option");
    o.value = sec.id; o.textContent = sec.id +
      (sec.detector ? ` (${sec.detector})` : "");
    ds.appendChild(o);
    if (sec.detector) {
      const od = make("option");
      od.value = sec.detector; od.textContent = sec.detector;
      dd.appendChild(od);
    }
  }
}

$("det-detector").addEventListener("change", () => {
  const v = $("det-detector").value;
  if (!v) return;
  const sec = state.station.station.sections.find((s) => s.detector === v);
  if (sec) $("det-section").value = sec.id;
});

$("btn-detect").addEventListener("click", async () => {
  const snap = state.station;
  let seq = parseInt($("det-seq").value, 10);
  if (!seq) seq = snap.sequence.next_expected_seq;
  const body = {
    seq, state: $("det-state").value,
    section: $("det-section").value || null,
    detector: $("det-detector").value || null,
  };
  try {
    const r = await api("/api/detections", {
      method: "POST", body: JSON.stringify(body)});
    renderDetectionResult(r);
    $("det-seq").value = "";
  } catch (e) {
    renderDetectionResult({error: e.data || {detail: e.message}});
  }
  await refresh();
});

function renderDetectionResult(r) {
  const box = $("verdict-box");
  box.innerHTML = "";
  const head = make("div");
  head.className = r.error ? "vdenied"
    : (r.effect === "FAULT_LOCK" ? "vdenied" : "vgranted");
  head.textContent = r.error ? `✘ ${r.error.detail || "上报被拒绝"}`
    : `序号 ${r.seq}: ${r.verdict || ""} ${r.effect || ""} ` +
      `${r.section || ""} ${r.detail || r.note || ""}`;
  box.appendChild(head);
  const pre = make("pre");
  pre.textContent = JSON.stringify(r, null, 2);
  box.appendChild(pre);
}

function renderActive() {
  const box = $("active-routes");
  box.innerHTML = "";
  const snap = state.station;
  if (!snap) return;
  for (const r of snap.active_routes) {
    if (r.state === "COMPLETED") continue;
    const card = make("div", "act-card" +
      (r.state === "FAULT" ? " fault" : ""));
    const head = make("div");
    head.innerHTML = "";
    html(head, make("strong"));
    head.querySelector("strong").textContent =
      `#${r.instance} ${r.route_id} `;
    const badge = make("span", "badge " + r.state.toLowerCase());
    badge.textContent = r.state === "FAULT" ? "故障锁闭" : "活动";
    head.appendChild(badge);
    if (r.fault_reason) {
      const fr = make("div", "meta");
      fr.style.color = "#e3aaf5";
      fr.textContent = r.fault_reason;
      card.appendChild(fr);
    }
    card.appendChild(head);

    const row = make("div", "sec-row");
    for (const s of r.sections) {
      const chip = make("span", "sec-chip " +
        (r.state === "FAULT" ? "fault"
         : s.global_occupied ? "occupied"
         : s.cleared ? "cleared" : ""));
      chip.textContent =
        `${s.index + 1}.${s.id}` +
        (s.global_occupied ? "●占" : s.cleared ? "○清" : "·锁");
      row.appendChild(chip);
    }
    card.appendChild(row);

    const sims = make("div", "sim-buttons");
    if (r.state === "ACTIVE") {
      const step = make("button");
      step.textContent = "▶ 模拟走行一步（自动序号）";
      step.addEventListener("click", () => simulateStep(r.instance));
      sims.appendChild(step);
      const full = make("button", "secondary");
      full.textContent = "⏩ 一次走完";
      full.addEventListener("click", () => simulateFull(r.instance));
      sims.appendChild(full);
    } else {
      const reset = make("button", "danger");
      reset.textContent = "故障复位（须全段空闲）";
      reset.addEventListener("click", () => resetRoute(r.instance));
      sims.appendChild(reset);
    }
    card.appendChild(sims);
    box.appendChild(card);
  }
}

// simulation: walk the route in order, two detections per section.
function simRecord(inst) {
  if (!state.sim.has(inst))
    state.sim.set(inst, {step: 0, n: null});
  return state.sim.get(inst);
}

async function simulateStep(inst) {
  const snap = state.station;
  const r = snap.active_routes.find((x) => x.instance === inst);
  if (!r) return;
  const n = r.sections.length;
  const rec = simRecord(inst);
  rec.n = n;
  // step 0..2n-1 : even = occupy k, odd = clear k
  const k = Math.floor(rec.step / 2);
  const isOccupy = rec.step % 2 === 0;
  const sec = r.sections[k];
  const seq = snap.sequence.next_expected_seq;
  const body = {seq, state: isOccupy ? "OCCUPY" : "CLEAR",
    section: sec.id, detector: sec.detector};
  rec.step += 1;
  if (rec.step >= 2 * n) state.sim.delete(inst);
  try {
    const res = await api("/api/detections", {
      method: "POST", body: JSON.stringify(body)});
    renderDetectionResult(res);
  } catch (e) {
    renderDetectionResult({error: e.data || {detail: e.message}});
  }
  await refresh();
}

async function simulateFull(inst) {
  for (let i = 0; i < 30; i++) {
    const before = state.station;
    const r = before.active_routes.find((x) => x.instance === inst);
    if (!r || r.state !== "ACTIVE") break;
    await simulateStep(inst);
  }
}

async function resetRoute(inst) {
  try {
    const r = await api("/api/routes/reset", {
      method: "POST", body: JSON.stringify({route: String(inst)})});
    renderDetectionResult({seq: "-", verdict: "RESET",
      effect: r.route_id, note: `#${r.route_instance} 已复位`});
  } catch (e) {
    renderDetectionResult({error: e.data || {detail: e.message}});
  }
  await refresh();
}

// ---------------------------------------------------------------------------
// events + replay
// ---------------------------------------------------------------------------

async function loadEvents() {
  const kind = $("event-filter").value;
  const q = new URLSearchParams({limit: "100"});
  if (kind) q.set("kind", kind);
  const data = await api("/api/events?" + q.toString());
  const box = $("event-log");
  box.innerHTML = "";
  if (!data.events.length) {
    box.textContent = "（无事件）";
    return;
  }
  for (const ev of data.events) {
    const row = make("div", "event-row");
    const eid = make("span", "eid"); eid.textContent = "#" + ev.id;
    const ts = make("span", "ts");
    ts.textContent = ev.ts.replace("T", " ").replace(/\..*$/, "");
    const kindNode = make("span", "event-kind ek-" +
      (ev.payload.verdict || ev.kind));
    let label = ev.kind;
    if (ev.kind === "ROUTE_REQUEST")
      label = (ev.payload.verdict === "GRANT" ? "GRANT 放行 " : "DENY 拒绝 ")
        + (ev.payload.route_id || "");
    else if (ev.kind === "DETECTION")
      label = `DETECTION #${ev.payload.seq} ${ev.payload.effect || ""} ` +
        `${ev.payload.section || ""}`;
    kindNode.textContent = label;
    const summary = make("span");
    summary.textContent = summarize(ev);
    row.append(eid, ts, kindNode, summary);
    row.addEventListener("click", () => {
      $("replay-id").value = ev.id;
      $("replay-modal").classList.remove("hidden");
      doReplay();
    });
    box.appendChild(row);
  }
}

function summarize(ev) {
  const p = ev.payload;
  if (ev.kind === "ROUTE_REQUEST" && p.verdict === "DENY")
    return (p.conflicts || []).map((c) => c.detail).join("；");
  if (ev.kind === "ROUTE_REQUEST")
    return `锁闭 ${(p.locked || []).join("，")}`;
  if (ev.kind === "DETECTION")
    return p.detail || (p.released && p.released.length
      ? "释放 " + p.released.map((r) => r.section).join("、")
      : "");
  if (ev.kind === "ROUTE_FAULT") return p.detail || p.fault_code;
  if (ev.kind === "ROUTE_COMPLETED") return `${p.route_id} 全段出清`;
  if (ev.kind === "ROUTE_RESET") return `${p.route_id} 已复位`;
  if (ev.kind === "SECTION_RELEASE") return `${p.route_id} 释放 ${p.section}`;
  if (ev.kind === "STATION_IMPORT") return `导入 ${p.name || ""}`;
  return "";
}

$("btn-refresh-events").addEventListener("click", loadEvents);
$("event-filter").addEventListener("change", loadEvents);

$("btn-replay").addEventListener("click", () => {
  $("replay-modal").classList.remove("hidden");
});
$("btn-close-replay").addEventListener("click", () =>
  $("replay-modal").classList.add("hidden"));

async function doReplay() {
  const id = parseInt($("replay-id").value, 10);
  if (!id) return;
  try {
    const r = await api(`/api/replay/${id}`);
    $("replay-output").textContent = JSON.stringify(
      pruneReplay(r), null, 2);
  } catch (e) {
    $("replay-output").textContent = "回放失败：" +
      (e.data && e.data.detail || e.message);
  }
}
function pruneReplay(r) {
  const {events, ...rest} = r;
  rest.event_count = events ? events.length : 0;
  return rest;
}
$("btn-do-replay").addEventListener("click", doReplay);
$("btn-replay-prev").addEventListener("click", () => {
  $("replay-id").value = Math.max(1,
    parseInt($("replay-id").value || "1", 10) - 1);
  doReplay();
});
$("btn-replay-next").addEventListener("click", () => {
  $("replay-id").value = parseInt($("replay-id").value || "0", 10) + 1;
  doReplay();
});

// ---------------------------------------------------------------------------
// banner + polling
// ---------------------------------------------------------------------------

function renderBanner() {
  const b = $("seq-banner");
  const snap = state.station;
  if (!snap) { b.classList.add("hidden"); return; }
  const s = snap.sequence;
  if (s.gap) {
    b.classList.remove("hidden");
    b.classList.remove("ok");
    b.textContent =
      `⛔ 检测序号缺 #${s.gap_missing_seq}（已连续应用到 #${s.applied_seq}；` +
      `乱序缓冲: ${s.buffered_seqs.join(", ") || "无"}）` +
      ` — 禁止新放行、解锁与故障复位`;
  } else {
    b.classList.remove("hidden");
    b.classList.add("ok");
    b.textContent = `序号正常：已连续应用 #${s.applied_seq}，` +
      `下一期望 #${s.next_expected_seq}`;
  }
}

async function refresh() {
  try {
    state.station = await api("/api/state");
  } catch {
    return;
  }
  drawDiagram();
  renderRoutes();
  renderDetectors();
  renderActive();
  renderBanner();
}

$("btn-refresh-events"); // noop touch
refresh().then(loadEvents);
setInterval(refresh, 2000);
setInterval(loadEvents, 5000);
