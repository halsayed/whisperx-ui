"use strict";
// The kubeconfig lives only in this browser (sessionStorage, or localStorage
// when "remember" is ticked) and is sent with every API call. The server keeps
// nothing between calls.
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const KEY = "whisperx-ui-conn";

let conn = null;      // {kubeconfig, context, namespace, inCluster}
let cluster = null;   // answer of /api/connect
let state = null;     // answer of /api/state
let info = {};
let currentTab = "setup";
let selectedRun = null;
let runsCache = [];
let timers = {};

// ------------------------------------------------------------ helpers ----
async function api(path, body = {}) {
  const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ conn, ...body }) });
  if (!r.ok) {
    let msg = `Request failed (${r.status})`;
    try { msg = (await r.json()).error || msg; } catch (e) { /* not JSON */ }
    throw new Error(msg);
  }
  return r;
}
const apiJson = async (path, body) => (await api(path, body)).json();

function toast(text, bad = false) {
  const t = $("#toast");
  t.textContent = text;
  t.className = "toast" + (bad ? " bad" : "");
  clearTimeout(timers.toast);
  timers.toast = setTimeout(() => t.classList.add("hidden"), bad ? 7000 : 3500);
}
const msg = (el, text, kind = "bad") => { el.innerHTML = text ? `<div class="msg ${kind}">${esc(text)}</div>` : ""; };
const badge = (text, kind = "") => `<span class="badge ${kind}">${esc(text)}</span>`;
function checks(el, rows) {
  el.innerHTML = rows.map((r) => `<li class="${r.ok ? "ok" : r.optional ? "opt" : "bad"}"><b>${esc(r.title)}</b>${r.message ? " — " + esc(r.message) : ""}</li>`).join("");
}
async function busy(btn, fn) {
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Working…";
  try { return await fn(); } catch (e) { toast(e.message, true); } finally { btn.disabled = false; btn.textContent = old; }
}
const fmtSize = (n) => n > 1e9 ? (n / 1e9).toFixed(1) + " GB" : n > 1e6 ? (n / 1e6).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1e3)) + " KB";
const fmtTime = (iso) => iso ? new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "";
function fmtDur(sec) {
  sec = Math.max(0, Math.round(sec));
  const m = Math.floor(sec / 60), s = sec % 60;
  return m >= 60 ? `${Math.floor(m / 60)} h ${m % 60} min` : m ? `${m} min ${s} s` : `${s} s`;
}
const STATE = { succeeded: ["Finished", "ok"], failed: ["Failed", "bad"], running: ["Running", "run"], waiting: ["Waiting", "warn run"] };

// ------------------------------------------------------------ connect ----
function show(view) {
  $("#view-connect").classList.toggle("hidden", view !== "connect");
  $("#view-app").classList.toggle("hidden", view !== "app");
  $("#conn-chip").classList.toggle("hidden", view !== "app");
}

function readFile(file) {
  if (!file) return;
  const r = new FileReader();
  r.onload = () => { $("#kc-text").value = r.result; $("#kc-name").textContent = "Loaded " + file.name; };
  r.readAsText(file);
}

async function doConnect(c, remember) {
  const data = await (async () => { conn = c; return apiJson("/api/connect"); })();
  cluster = data;
  conn.context = data.context;
  sessionStorage.setItem(KEY, JSON.stringify(conn));
  if (remember) localStorage.setItem(KEY, JSON.stringify(conn)); else localStorage.removeItem(KEY);
  $("#chip-text").textContent = `${data.server.replace(/^https?:\/\//, "")} · ${conn.namespace} · Kubernetes ${data.kubernetes || "?"}`;
  show("app");
  await loadState();
  const ready = state && state.config && state.secrets.s3 && state.storage.exists;
  switchTab(ready ? "files" : "setup");
}

async function connectClicked(inCluster) {
  const btn = inCluster ? $("#btn-incluster") : $("#btn-connect");
  const c = { namespace: $("#kc-ns").value.trim() || "whisperx" };
  if (inCluster) c.inCluster = true;
  else { c.kubeconfig = $("#kc-text").value; if (!$("#kc-ctx-wrap").classList.contains("hidden")) c.context = $("#kc-ctx").value; }
  msg($("#connect-msg"), "");
  const old = btn.textContent;
  btn.disabled = true; btn.textContent = "Connecting…";
  try { await doConnect(c, $("#kc-remember").checked); }
  catch (e) { conn = null; msg($("#connect-msg"), e.message); }
  finally { btn.disabled = false; btn.textContent = old; }
}

function parseContexts() {
  // light-weight peek so people with several clusters in one file can choose
  const names = [...$("#kc-text").value.matchAll(/^\s*-?\s*name:\s*(\S+)\s*$/gm)].map((m) => m[1]);
  const ctxBlock = $("#kc-text").value.split(/^contexts:/m)[1];
  let ctx = [];
  if (ctxBlock) ctx = [...ctxBlock.split(/^\S/m)[0].matchAll(/^\s*-?\s*name:\s*(\S+)\s*$/gm)].map((m) => m[1]);
  const cur = ($("#kc-text").value.match(/^current-context:\s*(\S+)/m) || [])[1];
  $("#kc-ctx-wrap").classList.toggle("hidden", ctx.length < 2);
  $("#kc-ctx").innerHTML = ctx.map((n) => `<option ${n === cur ? "selected" : ""}>${esc(n)}</option>`).join("");
  return names;
}

function disconnect() {
  sessionStorage.removeItem(KEY); localStorage.removeItem(KEY);
  conn = cluster = state = null; selectedRun = null;
  Object.values(timers).forEach(clearInterval);
  $("#kc-text").value = ""; $("#kc-name").textContent = "";
  show("connect");
}

// -------------------------------------------------------------- state ----
async function loadState() {
  state = await apiJson("/api/state");
  renderReady();
  fillSetup();
  return state;
}

function renderReady() {
  const s = state, chk = s.models.check;
  const items = [
    ["Model storage", s.storage.exists && s.storage.phase === "Bound" ? "ok" : s.storage.exists ? "warn" : "bad"],
    ["Models", chk && chk.state === "succeeded" ? "ok" : chk && chk.state === "failed" ? "bad" : "warn"],
    ["Object storage", s.secrets.s3 && s.config ? "ok" : "bad"],
  ];
  $("#ready").innerHTML = items.map(([t, k]) => badge((k === "ok" ? "✓ " : k === "bad" ? "✕ " : "? ") + t, k)).join("");
}

function fillSetup() {
  const f = $("#setup-form").elements, s = state, c = s.config || {}, st = s.settings || {};
  // storage
  $("#storage-existing").classList.toggle("hidden", !s.storage.exists);
  $("#storage-new").classList.toggle("hidden", s.storage.exists);
  if (s.storage.exists) {
    $("#storage-existing").textContent = `Connected to ${s.storage.nfsServer || "a volume"}${s.storage.nfsPath ? ":" + s.storage.nfsPath : ""} (${s.storage.phase === "Bound" ? "ready" : s.storage.phase || "pending"}).`;
  }
  $("#b-storage").innerHTML = s.storage.exists ? badge(s.storage.phase === "Bound" ? "Connected" : s.storage.phase || "Pending", s.storage.phase === "Bound" ? "ok" : "warn") : badge("Not set", "warn");
  renderModels();
  // s3
  if (s.config) {
    f.endpoint.value = c.S3_ENDPOINT_URL || ""; f.region.value = c.AWS_DEFAULT_REGION || "us-east-1";
    f.inputBucket.value = c.INPUT_BUCKET || ""; f.inputPrefix.value = c.INPUT_PREFIX || "";
    f.outputBucket.value = c.OUTPUT_BUCKET || ""; f.outputPrefix.value = c.OUTPUT_PREFIX || "";
    f.verifySsl.checked = c.S3_VERIFY_SSL !== "false";
    f.model.value = c.WHISPER_MODEL || "large-v3"; f.computeType.value = c.COMPUTE_TYPE || "float16";
    f.batchSize.value = c.BATCH_SIZE || 16;
    if (c.LANGUAGE && ![...f.language.options].some((o) => o.value === c.LANGUAGE)) f.language.add(new Option(c.LANGUAGE, c.LANGUAGE));
    f.language.value = c.LANGUAGE || "";
    f.diarize.checked = c.DIARIZE !== "false"; f.minSpeakers.value = c.MIN_SPEAKERS || ""; f.maxSpeakers.value = c.MAX_SPEAKERS || "";
    f.overwrite.checked = c.OVERWRITE === "true";
  }
  f.hostIp.value = st.s3HostIp || "";
  if (st.s3HostIp) f.hostIp.closest("details").open = true;
  f.restrictEgress.checked = s.egress;
  f.accessKey.placeholder = s.secrets.s3 ? `saved (${s.secrets.accessKeyHint}) — leave empty to keep` : "";
  f.secretKey.placeholder = s.secrets.s3 ? "saved — leave empty to keep" : "";
  $("#b-s3").innerHTML = s.secrets.s3 && s.config ? badge("Saved", "ok") : badge("Not set", "warn");
  // image
  f.image.value = st.image || info.defaultImage || "";
  f.registryUser.placeholder = s.secrets.pull ? `saved (${s.secrets.pullUser || "user"}) — leave empty to keep` : "";
  f.registryToken.placeholder = s.secrets.pull ? "saved — leave empty to keep" : "";
  $("#b-image").innerHTML = s.secrets.pull ? badge("Registry sign-in saved", "ok") : "";
  // gpu
  const opts = [`<option value="">Any available GPU</option>`].concat((cluster.gpus || []).map((g) => {
    const free = g.inUse == null ? "" : `, ${Math.max(0, g.total - g.inUse)} of ${g.total} free`;
    return `<option value="${esc(g.product)}">${esc(g.product.replace(/^NVIDIA-/, "").replace(/-/g, " "))} — ${Math.round(g.memoryMiB / 1024)} GB${free}</option>`;
  }));
  if (st.gpuProduct && !(cluster.gpus || []).some((g) => g.product === st.gpuProduct)) opts.push(`<option value="${esc(st.gpuProduct)}">${esc(st.gpuProduct)}</option>`);
  f.gpuProduct.innerHTML = opts.join("");
  f.gpuProduct.value = st.gpuProduct || "";
  $("#speakers").classList.toggle("hidden", !f.diarize.checked);
  gpuHint(false);
}

function gpuHint(apply) {
  const f = $("#setup-form").elements, g = (cluster.gpus || []).find((x) => x.product === f.gpuProduct.value), el = $("#gpu-hint");
  if (!g) { el.classList.add("hidden"); return; }
  const small = g.memoryMiB && g.memoryMiB <= 8192;
  if (apply) { f.computeType.value = small ? "int8_float16" : "float16"; f.batchSize.value = small ? 4 : 16; }
  const busyNow = g.inUse != null && g.inUse >= g.total;
  el.classList.remove("hidden");
  el.textContent = (small ? "Small GPU: Compact precision and batch size 4 are recommended." : "Large GPU: Standard precision and batch size 16 are recommended.")
    + (busyNow ? " This GPU type is fully in use right now, so a run will wait until one is free." : "");
}

function renderModels() {
  const chk = state.models.check, dl = state.models.download;
  let text = "Not checked yet.";
  if (chk) {
    if (chk.state === "succeeded") text = `All required models are on the share (checked ${fmtTime(chk.finished)}).`;
    else if (chk.state === "failed") text = "Some required models are missing.";
    else text = "Checking… " + (chk.detail || "");
  }
  $("#check-status").textContent = text;
  checks($("#check-items"), ((chk && chk.items) || []).map((i) => ({ ok: i.ok, optional: i.optional, title: i.label, message: i.ok ? "" : i.optional ? "not on the share (not needed with the current settings)" : "missing" })));
  $("#btn-download-log").classList.toggle("hidden", !dl);
  $("#download-status").textContent = dl ? ({ succeeded: "Download finished.", failed: "Download failed — see progress.", running: "Downloading…", waiting: "Starting… " + (dl.detail || "") }[dl.state]) : "";
  const pending = [chk, dl].some((j) => j && (j.state === "running" || j.state === "waiting"));
  clearTimeout(timers.models);
  if (pending && currentTab === "setup") timers.models = setTimeout(async () => { try { state.models = (await apiJson("/api/state")).models; renderReady(); renderModels(); } catch (e) { /* retry on next action */ } }, 4000);
}

function formS3() {
  const f = $("#setup-form").elements;
  return { endpoint: f.endpoint.value, region: f.region.value, verifySsl: f.verifySsl.checked, hostIp: f.hostIp.value, accessKey: f.accessKey.value,
    secretKey: f.secretKey.value, inputBucket: f.inputBucket.value, inputPrefix: f.inputPrefix.value, outputBucket: f.outputBucket.value, outputPrefix: f.outputPrefix.value };
}

async function saveSetup(ev) {
  ev.preventDefault();
  const f = $("#setup-form").elements;
  const body = {
    storage: { nfsServer: f.nfsServer.value, nfsPath: f.nfsPath.value },
    s3: formS3(),
    image: { name: f.image.value, registryUser: f.registryUser.value, registryToken: f.registryToken.value, registryServer: f.registryServer.value },
    options: { model: f.model.value, computeType: f.computeType.value, batchSize: f.batchSize.value, language: f.language.value, diarize: f.diarize.checked,
      minSpeakers: f.minSpeakers.value, maxSpeakers: f.maxSpeakers.value, overwrite: f.overwrite.checked },
    gpuProduct: f.gpuProduct.value,
    restrictEgress: f.restrictEgress.checked,
  };
  await busy($("#btn-save"), async () => {
    const res = await apiJson("/api/setup", body);
    checks($("#save-steps"), res.steps.map((s) => ({ ok: s.ok, title: s.step, message: s.message })));
    $("#save-status").textContent = res.ok ? "Saved. Next: add recordings and start a run." : "Saved, but some items still need attention.";
    ["accessKey", "secretKey", "registryToken", "registryUser"].forEach((n) => { f[n].value = ""; });
    await loadState();
  });
}

// --------------------------------------------------------- recordings ----
async function loadFiles() {
  const tb = $("#files-table tbody");
  try {
    const d = await apiJson("/api/s3/files");
    msg($("#files-msg"), "");
    $("#files-where").textContent = `From ${d.inputBucket}/${d.inputPrefix} → results in ${d.outputBucket}/${d.outputPrefix}`;
    const waiting = d.files.filter((x) => !x.done).length;
    if (!d.files.length) { $("#files-table").classList.add("hidden"); msg($("#files-msg"), "No recordings yet. Upload some audio files above.", "warn"); return; }
    $("#files-table").classList.remove("hidden");
    if (waiting) msg($("#files-msg"), `${waiting} of ${d.files.length} recording(s) are waiting to be transcribed. Press "Start transcription".`, "warn");
    const labels = { ".dialog.txt": "Text", ".json": "JSON", ".srt": "SRT", ".vtt": "VTT" };
    tb.innerHTML = d.files.map((x) => `<tr data-name="${esc(x.name)}">
      <td>${esc(x.name)}</td><td>${fmtSize(x.size)}</td><td>${esc(fmtTime(x.modified))}</td>
      <td>${x.done ? badge("Transcribed", "ok") : badge("Waiting", "warn")}</td>
      <td class="actions">${x.results.includes(".dialog.txt") ? `<button class="mini primary" data-view>Read</button> ` : ""}${x.results.map((e) => `<button class="mini" data-dl="${e}">${labels[e]}</button>`).join(" ")}</td>
      <td class="actions"><button class="mini ghost danger" data-del>Delete</button></td></tr>`).join("");
  } catch (e) { $("#files-table").classList.add("hidden"); msg($("#files-msg"), e.message); }
}

async function uploadFiles(list) {
  const files = [...list];
  if (!files.length) return;
  const st = $("#up-status");
  let done = 0;
  for (const file of files) {
    st.textContent = `Uploading ${file.name} (${done + 1} of ${files.length})…`;
    const fd = new FormData();
    fd.append("conn", JSON.stringify(conn));
    fd.append("files", file, file.name);
    try {
      const r = await fetch("/api/s3/upload", { method: "POST", body: fd });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || `Upload failed (${r.status})`);
      done++;
    } catch (e) { toast(e.message, true); }
  }
  st.textContent = `${done} of ${files.length} file(s) uploaded.`;
  loadFiles();
}

async function fileAction(ev) {
  const row = ev.target.closest("tr"); if (!row) return;
  const name = row.dataset.name;
  if (ev.target.dataset.view !== undefined) {
    try {
      const text = await (await api("/api/s3/result", { name, ext: ".dialog.txt" })).text();
      $("#modal-title").textContent = name; $("#modal-body").textContent = text || "(empty transcript)"; $("#modal").classList.remove("hidden");
    } catch (e) { toast(e.message, true); }
  } else if (ev.target.dataset.dl) {
    try {
      const ext = ev.target.dataset.dl;
      const blob = await (await api("/api/s3/result", { name, ext, download: true })).blob();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob); a.download = name.split("/").pop().replace(/\.[^.]+$/, "") + ext; a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    } catch (e) { toast(e.message, true); }
  } else if (ev.target.dataset.del !== undefined) {
    if (!confirm(`Delete the recording "${name}" and its results from object storage? This cannot be undone.`)) return;
    try { await api("/api/s3/delete", { name }); loadFiles(); } catch (e) { toast(e.message, true); }
  }
}

// --------------------------------------------------------------- runs ----
async function startRun(ev) {
  await busy(ev.target, async () => {
    const r = await apiJson("/api/runs/start");
    selectedRun = r.name;
    toast("Run started");
    switchTab("runs");
  });
}

async function loadRuns() {
  try {
    runsCache = (await apiJson("/api/runs")).runs;
    msg($("#runs-msg"), "");
  } catch (e) { msg($("#runs-msg"), e.message); return; }
  if (!runsCache.length) { $("#runs-list").innerHTML = `<p class="muted">No runs yet. Upload recordings, then press "Start transcription".</p>`; $("#run-detail").classList.add("hidden"); return; }
  if (!selectedRun || !runsCache.some((r) => r.name === selectedRun)) selectedRun = runsCache[0].name;
  $("#runs-list").innerHTML = runsCache.map((r) => {
    const [label, kind] = STATE[r.state] || [r.state, ""];
    const dur = r.started ? fmtDur(((r.finished ? new Date(r.finished) : new Date()) - new Date(r.started)) / 1000) : "";
    return `<div class="run ${r.name === selectedRun ? "sel" : ""}" data-run="${esc(r.name)}">
      ${badge(label, kind)}<span class="name">${esc(r.name)}</span><span class="muted small">${esc(fmtTime(r.created))}${dur ? " · " + dur : ""}${r.node ? " · on " + esc(r.node) : ""}</span>
      ${r.detail && r.state !== "succeeded" ? `<span class="detail">${esc(r.detail)}</span>` : ""}</div>`;
  }).join("");
  await loadLog();
}

function parseLog(text) {
  const out = { total: null, todo: null, files: [], summary: null, gpu: null, errors: [] };
  for (const line of text.split("\n")) {
    let m;
    if ((m = line.match(/found (\d+) audio file\(s\), (\d+) to process/))) { out.total = +m[1]; out.todo = +m[2]; }
    else if ((m = line.match(/INFO GPU: (.*)$/))) out.gpu = m[1];
    else if ((m = line.match(/INFO OK (\{.*\})\s*$/))) { try { out.files.push(JSON.parse(m[1])); } catch (e) { /* partial line */ } }
    else if ((m = line.match(/ERROR FAILED (.*)$/))) out.files.push({ file: m[1], status: "failed" });
    else if ((m = line.match(/INFO SUMMARY (\{.*\})\s*$/))) { try { out.summary = JSON.parse(m[1]); } catch (e) { /* partial line */ } }
    else if (/Error|Exception|exit|denied/i.test(line) && !/Traceback/.test(line)) out.errors.push(line);
  }
  return out;
}

async function loadLog() {
  const run = runsCache.find((r) => r.name === selectedRun);
  const box = $("#run-detail");
  if (!run) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  $("#run-title").textContent = run.name;
  $("#btn-run-stop").textContent = run.state === "running" || run.state === "waiting" ? "Stop this run" : "Remove from list";
  let text = "";
  try { text = (await apiJson("/api/logs", { name: run.name })).log; } catch (e) { text = ""; }
  if (selectedRun !== run.name) return;
  const p = parseLog(text), prog = $("#run-progress");
  const doneCount = p.files.length;
  let html = "";
  if (run.state === "waiting") html = `<div class="msg warn">${esc(run.detail || "Waiting to start…")}</div>`;
  else if (p.todo === 0) html = `<div class="msg ok">Nothing to do: all ${p.total} recording(s) already have results.</div>`;
  else if (p.todo != null) {
    const pct = Math.round((doneCount / p.todo) * 100);
    html = `<div><b>${doneCount} of ${p.todo}</b> recording(s) processed${p.gpu ? ` · GPU: ${esc(p.gpu)}` : ""}${run.state === "running" && doneCount < p.todo ? (doneCount ? "" : " · loading models…") : ""}</div><div class="bar"><i style="width:${pct}%"></i></div>`;
  } else if (run.state === "running") html = `<div class="muted">Starting up…</div>`;
  if (p.summary) html += `<div class="msg ${p.summary.failed ? "bad" : "ok"}">${p.summary.files - p.summary.failed} of ${p.summary.files} recording(s) transcribed${p.summary.failed ? `, ${p.summary.failed} failed` : ""} · ${fmtDur(p.summary.audio_s)} of audio in ${fmtDur(p.summary.wall_s)}${p.summary.gpu_peak_gib ? ` · peak GPU memory ${p.summary.gpu_peak_gib} GB` : ""}</div>`;
  else if (run.state === "failed") html += `<div class="msg bad">The run failed. ${esc(run.detail || p.errors.slice(-1)[0] || "Tick 'Show full technical log' for details.")}</div>`;
  prog.innerHTML = html;
  $("#run-files").classList.toggle("hidden", !p.files.length);
  $("#run-files tbody").innerHTML = p.files.map((f) => `<tr><td>${esc(String(f.file).split("/").pop())}</td>
    <td>${f.status === "ok" ? badge("Done", "ok") : badge("Failed", "bad")}</td><td>${esc(f.language || "")}</td>
    <td>${f.duration_s != null ? fmtDur(f.duration_s) : ""}</td><td>${f.wall_s != null ? fmtDur(f.wall_s) : ""}</td>
    <td>${f.speed_x ? esc(f.speed_x) + "× real time" : ""}</td><td>${f.speakers ?? ""}</td></tr>`).join("");
  const pre = $("#run-log"), raw = $("#log-raw").checked || (run.state === "failed" && !p.summary);
  pre.classList.toggle("hidden", !raw);
  if (raw) { const atEnd = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 30; pre.textContent = text || "(no log output yet)"; if (atEnd) pre.scrollTop = pre.scrollHeight; }
}

async function stopRun() {
  const run = runsCache.find((r) => r.name === selectedRun); if (!run) return;
  const active = run.state === "running" || run.state === "waiting";
  if (!confirm(active ? `Stop the run ${run.name}? Recordings already transcribed keep their results.` : `Remove ${run.name} and its log from the list?`)) return;
  try { await api("/api/runs/delete", { name: run.name }); selectedRun = null; setTimeout(loadRuns, 800); } catch (e) { toast(e.message, true); }
}

// --------------------------------------------------------------- tabs ----
function switchTab(tab) {
  currentTab = tab;
  $$(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  $$(".tabpane").forEach((p) => p.classList.toggle("hidden", p.id !== "tab-" + tab));
  clearInterval(timers.poll);
  if (tab === "files") loadFiles();
  if (tab === "runs") { loadRuns(); timers.poll = setInterval(() => { if (!document.hidden) loadRuns(); }, 4000); }
  if (tab === "setup" && state) loadState().catch((e) => toast(e.message, true));
}

// --------------------------------------------------------------- init ----
function dropZone(el, onFiles) {
  el.addEventListener("dragover", (e) => { e.preventDefault(); el.classList.add("over"); });
  el.addEventListener("dragleave", () => el.classList.remove("over"));
  el.addEventListener("drop", (e) => { e.preventDefault(); el.classList.remove("over"); onFiles(e.dataTransfer.files); });
}

async function init() {
  info = await (await fetch("/api/info")).json();
  $("#version").textContent = "WhisperX Pipeline UI " + info.version;
  $("#btn-incluster").classList.toggle("hidden", !info.inCluster);

  dropZone($("#drop"), (f) => { readFile(f[0]); setTimeout(parseContexts, 200); });
  $("#kc-file").addEventListener("change", (e) => { readFile(e.target.files[0]); setTimeout(parseContexts, 200); });
  $("#kc-text").addEventListener("input", parseContexts);
  $("#btn-connect").addEventListener("click", () => connectClicked(false));
  $("#btn-incluster").addEventListener("click", () => connectClicked(true));
  $("#btn-disconnect").addEventListener("click", disconnect);
  $$(".tab").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));

  $("#setup-form").addEventListener("submit", saveSetup);
  $("#setup-form").elements.gpuProduct.addEventListener("change", () => gpuHint(true));
  $("#setup-form").elements.diarize.addEventListener("change", (e) => $("#speakers").classList.toggle("hidden", !e.target.checked));
  $("#btn-s3test").addEventListener("click", (e) => busy(e.target, async () => {
    $("#s3-results").innerHTML = "";
    const r = await apiJson("/api/s3/test", { s3: formS3() });
    checks($("#s3-results"), r.results.map((x) => ({ ok: x.ok, title: x.check, message: x.ok ? "works" : x.message })));
  }));
  $("#btn-check").addEventListener("click", (e) => busy(e.target, async () => { await api("/api/models/check"); await loadState(); }));
  $("#btn-download").addEventListener("click", (e) => busy(e.target, async () => {
    await api("/api/models/download", { hfToken: $("#hf-token").value }); $("#hf-token").value = ""; await loadState();
  }));
  $("#btn-download-log").addEventListener("click", async () => {
    try {
      const r = await apiJson("/api/logs", { name: "model-download" });
      $("#modal-title").textContent = "Model download progress"; $("#modal-body").textContent = r.log || "(no output yet)"; $("#modal").classList.remove("hidden");
    } catch (e) { toast(e.message, true); }
  });

  dropZone($("#updrop"), uploadFiles);
  $("#up-file").addEventListener("change", (e) => { uploadFiles(e.target.files); e.target.value = ""; });
  $("#btn-files-refresh").addEventListener("click", loadFiles);
  $("#files-table").addEventListener("click", fileAction);
  $$("[data-start]").forEach((b) => b.addEventListener("click", startRun));
  $("#runs-list").addEventListener("click", (e) => { const r = e.target.closest("[data-run]"); if (r) { selectedRun = r.dataset.run; loadRuns(); } });
  $("#btn-run-stop").addEventListener("click", stopRun);
  $("#log-raw").addEventListener("change", loadLog);
  $("#modal-close").addEventListener("click", () => $("#modal").classList.add("hidden"));
  $("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") $("#modal").classList.add("hidden"); });

  const saved = sessionStorage.getItem(KEY) || localStorage.getItem(KEY);
  if (saved) {
    try { const c = JSON.parse(saved); $("#kc-ns").value = c.namespace || "whisperx"; await doConnect(c, !!localStorage.getItem(KEY)); return; }
    catch (e) { conn = null; msg($("#connect-msg"), "Could not reconnect: " + e.message); }
  }
  show("connect");
}
init();
