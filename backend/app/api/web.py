"""Open (no-auth) web upload pages for large files and folders.

Two pages share the same in-browser flow:

* ``GET /upload``      - the user pastes the API key into the form.
* ``GET /u/{token}``   - a keyless, single-use link (createUploadLink); the URL
  path token authorizes the upload, so the user never sees the API key.

Both collect a folder / multiple files, zip them in-browser (JSZip) into one
bundle.zip (or upload a single file directly), then initUpload -> PUT bytes
(token) -> build-from-upload-ref -> poll -> download / copy the OSS share link.
Bytes go straight to the server, never through GPT/Claude.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.core import upload_links

router = APIRouter(tags=["web"])

# --- Shared <head> (JSZip + styles) ----------------------------------------
_HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Safe Memory - Upload</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/jszip/3.10.1/jszip.min.js"></script>
<style>
  :root { --bg:#0f172a; --card:#1e293b; --fg:#e2e8f0; --muted:#94a3b8;
          --accent:#38bdf8; --ok:#22c55e; --err:#ef4444; --border:#334155; }
  * { box-sizing: border-box; }
  body { margin:0; font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
         background:var(--bg); color:var(--fg); }
  .wrap { max-width:640px; margin:0 auto; padding:24px 16px 64px; }
  h1 { font-size:1.4rem; margin:8px 0 4px; }
  p.sub { color:var(--muted); margin:0 0 20px; font-size:.9rem; }
  .card { background:var(--card); border:1px solid var(--border);
          border-radius:12px; padding:18px; margin-bottom:16px; }
  label { display:block; font-size:.8rem; color:var(--muted); margin:10px 0 4px; }
  input, select { width:100%; padding:10px; border-radius:8px; border:1px solid var(--border);
                  background:#0b1220; color:var(--fg); font-size:.95rem; }
  .row { display:flex; gap:12px; } .row > div { flex:1; }
  #drop { border:2px dashed var(--border); border-radius:10px; padding:22px;
          text-align:center; color:var(--muted); cursor:pointer; margin-top:6px; }
  #drop.hot { border-color:var(--accent); color:var(--accent); }
  .picks { display:flex; gap:10px; margin-top:8px; }
  .picks button { margin:0; flex:1; padding:9px; background:#0b1220; color:var(--fg);
                  border:1px solid var(--border); font-weight:500; font-size:.85rem; }
  button { margin-top:16px; width:100%; padding:12px; border:none; border-radius:8px;
           background:var(--accent); color:#04283a; font-weight:600; font-size:1rem; cursor:pointer; }
  button:disabled { opacity:.5; cursor:not-allowed; }
  #status { margin-top:14px; font-size:.9rem; white-space:pre-wrap; }
  .ok { color:var(--ok); } .err { color:var(--err); }
  .result { margin-top:12px; font-size:.9rem; }
  code { background:#0b1220; padding:2px 6px; border-radius:5px; word-break:break-all; }
  a.dl { display:inline-block; margin-top:10px; padding:10px 14px; background:var(--ok);
         color:#04240f; border-radius:8px; text-decoration:none; font-weight:600; cursor:pointer; }
  .share { margin-top:12px; padding:10px; border:1px solid var(--border); border-radius:8px;
           background:#0b1220; }
  .share .u { font-size:.8rem; color:var(--accent); display:block; margin:6px 0; }
  .share button { margin-top:6px; padding:8px; background:var(--accent); color:#04283a; }
  #flist { max-height:120px; overflow:auto; margin-top:6px; font-size:.8rem; color:var(--muted); }
</style>
</head>"""

# --- Shared file-collection JS (pickers, drag-drop, in-browser zip) ---------
_COLLECT_JS = """
const $ = (id) => document.getElementById(id);
const drop = $("drop"), fileInput = $("file"), folderInput = $("folder");
const ALLOWED = [".txt",".md",".csv",".tsv",".json",".xlsx",".xls",".docx",".pptx",".pdf",".png",".jpg",".jpeg",".tiff",".tif",".bmp",".webp"];
// collected: array of { file, path } where path is the relative path in the bundle.
let collected = [];

function extOk(name) {
  const n = (name || "").toLowerCase();
  return ALLOWED.some(e => n.endsWith(e));
}
function isJunk(name) {
  const b = (name || "").split("/").pop();
  return !b || b.startsWith(".") || b.startsWith("__MACOSX")
      || b === "Thumbs.db" || b === "desktop.ini" || b.startsWith("~$");
}

function summarize(items, skipped) {
  collected = items;
  if (!items.length) {
    $("fname").textContent = skipped ? (skipped + " file(s) skipped, none supported") : "";
    $("flist").innerHTML = "";
    return;
  }
  const total = items.reduce((s, it) => s + (it.file.size || 0), 0);
  $("fname").textContent = items.length + " file(s), " + (total/1048576).toFixed(2)
    + " MB" + (skipped ? (" \\u2014 " + skipped + " skipped") : "");
  $("flist").innerHTML = items.slice(0, 50).map(it => "\\u2022 " + it.path).join("<br>")
    + (items.length > 50 ? "<br>\\u2026" : "");
}

// --- File input pickers -----------------------------------------------------
function fromFileList(files, useRelPath) {
  const out = []; let skipped = 0;
  for (const f of files) {
    const rel = useRelPath && f.webkitRelativePath ? f.webkitRelativePath : f.name;
    if (isJunk(rel) || !extOk(rel)) { skipped++; continue; }
    out.push({ file: f, path: rel });
  }
  return { out, skipped };
}
$("pickFolder").onclick = () => folderInput.click();
$("pickFiles").onclick = () => fileInput.click();
folderInput.onchange = () => { const r = fromFileList(folderInput.files, true); summarize(r.out, r.skipped); };
fileInput.onchange = () => { const r = fromFileList(fileInput.files, false); summarize(r.out, r.skipped); };
drop.onclick = () => fileInput.click();

// --- Drag & drop with recursive folder traversal ---------------------------
function readEntries(reader) {
  return new Promise((res, rej) => reader.readEntries(res, rej));
}
async function walkEntry(entry, prefix, acc) {
  if (entry.isFile) {
    const file = await new Promise((res, rej) => entry.file(res, rej));
    acc.push({ file, path: prefix + entry.name });
  } else if (entry.isDirectory) {
    const reader = entry.createReader();
    let batch;
    do {
      batch = await readEntries(reader);
      for (const child of batch) await walkEntry(child, prefix + entry.name + "/", acc);
    } while (batch.length);
  }
}
async function collectFromDataTransfer(dt) {
  const acc = [];
  const items = dt.items ? Array.from(dt.items) : [];
  const entries = items.map(it => it.webkitGetAsEntry && it.webkitGetAsEntry()).filter(Boolean);
  if (entries.length) {
    for (const e of entries) await walkEntry(e, "", acc);
  } else if (dt.files) {
    for (const f of dt.files) acc.push({ file: f, path: f.name });
  }
  let skipped = 0;
  const out = acc.filter(it => {
    if (isJunk(it.path) || !extOk(it.path)) { skipped++; return false; }
    return true;
  });
  return { out, skipped };
}
["dragover","dragenter"].forEach(e => drop.addEventListener(e, ev => { ev.preventDefault(); drop.classList.add("hot"); }));
["dragleave"].forEach(e => drop.addEventListener(e, ev => { ev.preventDefault(); drop.classList.remove("hot"); }));
drop.addEventListener("drop", async ev => {
  ev.preventDefault(); drop.classList.remove("hot");
  setStatus("Reading dropped items...");
  const r = await collectFromDataTransfer(ev.dataTransfer);
  summarize(r.out, r.skipped);
  setStatus("");
});

function setStatus(msg, cls) { const s = $("status"); s.textContent = msg; s.className = cls || ""; }
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

// Build the upload payload: a single file uploads directly; 2+ files (or a
// folder) are zipped into one bundle.zip so the server merges them into 1 pack.
async function buildPayload() {
  if (collected.length === 1) {
    const only = collected[0].file;
    return { blob: only, filename: only.name, contentType: only.type || "application/octet-stream" };
  }
  if (typeof JSZip === "undefined") throw new Error("JSZip failed to load (check network).");
  const zip = new JSZip();
  for (const it of collected) zip.file(it.path, it.file);
  const blob = await zip.generateAsync({ type: "blob", compression: "DEFLATE" });
  return { blob, filename: "bundle.zip", contentType: "application/zip" };
}
"""

# --- Body for the key-based /upload page -----------------------------------
_UPLOAD_BODY = """<body>
<div class="wrap">
  <h1>Safe Memory &mdash; Upload Folder or Files</h1>
  <p class="sub">Drop a <b>folder</b> or <b>multiple files</b> &mdash; they are zipped in your
  browser and merged into <b>one</b> Safe Memory Pack. A single file works too. Bytes go
  straight to the server, never through GPT/Claude. No cloud sign-in required.</p>

  <div class="card">
    <label>API key (X-Safe-Memory-Key)</label>
    <input id="apiKey" type="password" placeholder="your Safe Memory API key" />
    <div class="row">
      <div><label>agent_id</label><input id="agentId" value="tax-agent" /></div>
      <div><label>pack_id</label><input id="packId" value="my-pack" /></div>
    </div>
    <label>title</label>
    <input id="title" value="Uploaded Pack" />
    <div class="row">
      <div>
        <label>retention_mode</label>
        <select id="retention">
          <option value="process_and_return">process_and_return</option>
          <option value="server_vault">server_vault</option>
          <option value="session">session</option>
        </select>
      </div>
      <div><label>source_language</label><input id="lang" value="ja" /></div>
    </div>
    <label>Folder or files</label>
    <div id="drop">Drop a folder or files here, or use the buttons below<br><small id="fname"></small></div>
    <div class="picks">
      <button id="pickFolder" type="button">Choose folder&hellip;</button>
      <button id="pickFiles" type="button">Choose files&hellip;</button>
    </div>
    <div id="flist"></div>
    <input id="folder" type="file" multiple webkitdirectory directory style="display:none" />
    <input id="file" type="file" multiple accept=".txt,.md,.csv,.tsv,.json,.xlsx,.xls,.docx,.pptx,.pdf,.png,.jpg,.jpeg,.tiff,.tif,.bmp,.webp" style="display:none" />
    <button id="go">Upload &amp; Build Pack</button>
    <div id="status"></div>
    <div id="result" class="result"></div>
  </div>
</div>"""

# --- Run + render JS for the key-based /upload page ------------------------
_UPLOAD_RUN_JS = """
async function run() {
  const key = $("apiKey").value.trim();
  if (!collected.length) { setStatus("Please choose a folder or files.", "err"); return; }
  const authHeaders = key ? { "X-Safe-Memory-Key": key } : {};
  $("go").disabled = true; $("result").innerHTML = "";
  try {
    setStatus("1/5 Preparing bundle...");
    const payload = await buildPayload();

    setStatus("2/5 Initializing upload...");
    let r = await fetch("/api/uploads/init", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders },
      body: JSON.stringify({ filename: payload.filename, content_type: payload.contentType, size: payload.blob.size })
    });
    if (!r.ok) throw new Error("init failed: " + r.status + " " + await r.text());
    const init = await r.json();

    setStatus("3/5 Uploading " + payload.filename + " (" + (payload.blob.size/1048576).toFixed(2) + " MB)...");
    r = await fetch(init.upload_url + "?token=" + encodeURIComponent(init.upload_token), {
      method: "PUT",
      headers: { "Content-Type": "application/octet-stream" },
      body: payload.blob
    });
    if (!r.ok) throw new Error("upload failed: " + r.status + " " + await r.text());

    setStatus("4/5 Starting processing...");
    r = await fetch("/api/packs/build-from-upload-ref", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders },
      body: JSON.stringify({
        upload_id: init.upload_id,
        agent_id: $("agentId").value.trim(),
        pack_id: $("packId").value.trim(),
        title: $("title").value.trim(),
        source_language: $("lang").value.trim() || null,
        retention_mode: $("retention").value
      })
    });
    if (!r.ok) throw new Error("build-ref failed: " + r.status + " " + await r.text());
    const ref = await r.json();

    setStatus("5/5 Processing... (job " + ref.job_id + ")");
    let job = null;
    for (let i = 0; i < 150; i++) {
      await sleep(2000);
      const jr = await fetch("/api/jobs/" + ref.job_id, { headers: authHeaders });
      if (!jr.ok) throw new Error("job poll failed: " + jr.status);
      job = await jr.json();
      if (job.status === "COMPLETED" || job.status === "FAILED") break;
      setStatus("5/5 Processing... status=" + job.status);
    }
    if (!job || job.status !== "COMPLETED") {
      throw new Error("Processing did not complete: " + (job ? (job.status + " " + (job.warnings||[]).join("; ")) : "timeout"));
    }

    setStatus("Done!", "ok");
    renderResult(job, authHeaders);
  } catch (e) {
    setStatus(String(e.message || e), "err");
  } finally {
    $("go").disabled = false;
  }
}

function renderResult(job, authHeaders) {
  const el = $("result");
  const counts = job.classification_counts || {};
  let html = "<div><b>Pack:</b> <code>" + job.pack_id + "</code></div>";
  html += "<div><b>Files merged:</b> " + collected.length + "</div>";
  html += "<div><b>Entries:</b> " + job.entry_count + "</div>";
  html += "<div><b>Input type:</b> " + (job.input_type || "file") + "</div>";
  html += "<div><b>Classifications:</b> " + JSON.stringify(counts) + "</div>";
  html += "<div><b>Retention:</b> " + job.retention_mode + "</div>";
  const unsup = job.unsupported_files || [];
  if (unsup.length) {
    html += "<div class='err'>Skipped (unsupported): "
         + unsup.map(u => u.filename).join(", ") + "</div>";
  }
  if (job.retention_mode === "server_vault") {
    html += "<div class='ok'>Stored in the agent vault (queryable later).</div>";
  }
  if (job.download_url) {
    const shareUrl = job.download_url.startsWith("http")
      ? job.download_url : (location.origin + job.download_url);
    html += "<div class='share'><b>Share link (reuse this pack):</b>"
         + "<code class='u' id='shareUrl'>" + shareUrl + "</code>"
         + "<button id='copyBtn' type='button'>Copy share link</button>"
         + "<div style='font-size:.75rem;color:var(--muted);margin-top:6px'>"
         + "Paste this URL into GPT's <code>importPackByRef</code> to reuse the pack."
         + (job.download_url.startsWith("http") ? "" :
            " (This is a local link \\u2014 it needs the X-Safe-Memory-Key header; enable OSS for a self-contained signed URL.)")
         + "</div></div>";
    html += "<a class='dl' id='dlBtn'>Download pack (.smp.json)</a>";
  }
  el.innerHTML = html;
  const copy = $("copyBtn");
  if (copy) copy.onclick = async () => {
    try { await navigator.clipboard.writeText($("shareUrl").textContent); copy.textContent = "Copied!"; }
    catch (e) { copy.textContent = "Copy failed"; }
  };
  const dl = $("dlBtn");
  if (dl) {
    dl.onclick = async () => {
      const r = await fetch(job.download_url, { headers: authHeaders });
      if (!r.ok) { alert("Download failed: " + r.status); return; }
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = job.pack_id + ".smp.json";
      document.body.appendChild(a); a.click(); a.remove();
      URL.revokeObjectURL(url);
    };
  }
}
$("go").onclick = run;
"""

# --- Body for the keyless one-time /u/{token} page -------------------------
_TOKEN_BODY = """<body>
<div class="wrap">
  <h1>Safe Memory &mdash; Secure Upload Link</h1>
  <p class="sub">Drop a <b>folder</b> or <b>multiple files</b> &mdash; they are zipped in your
  browser and merged into <b>one</b> Safe Memory Pack. A single file works too.
  <b>No login or API key needed</b> &mdash; this is a secure, single-use link. Bytes go
  straight to the server, never through GPT/Claude.</p>

  <div class="card">
    <label>Folder or files</label>
    <div id="drop">Drop a folder or files here, or use the buttons below<br><small id="fname"></small></div>
    <div class="picks">
      <button id="pickFolder" type="button">Choose folder&hellip;</button>
      <button id="pickFiles" type="button">Choose files&hellip;</button>
    </div>
    <div id="flist"></div>
    <input id="folder" type="file" multiple webkitdirectory directory style="display:none" />
    <input id="file" type="file" multiple accept=".txt,.md,.csv,.tsv,.json,.xlsx,.xls,.docx,.pptx,.pdf,.png,.jpg,.jpeg,.tiff,.tif,.bmp,.webp" style="display:none" />
    <button id="go">Upload &amp; Build Pack</button>
    <div id="status"></div>
    <div id="result" class="result"></div>
  </div>
</div>"""

# --- Run + render JS for the keyless one-time /u/{token} page ---------------
_TOKEN_RUN_JS = """
// The one-time token is the last path segment (/u/{token}); it authorizes
// staging + a single scoped build. The master API key is never present here.
const TOKEN = decodeURIComponent((location.pathname.split("/").filter(Boolean).pop()) || "");

async function run() {
  if (!collected.length) { setStatus("Please choose a folder or files.", "err"); return; }
  const authHeaders = { "X-Upload-Token": TOKEN };
  $("go").disabled = true; $("result").innerHTML = "";
  try {
    setStatus("1/5 Preparing bundle...");
    const payload = await buildPayload();

    setStatus("2/5 Initializing upload...");
    let r = await fetch("/api/uploads/init", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders },
      body: JSON.stringify({ filename: payload.filename, content_type: payload.contentType, size: payload.blob.size })
    });
    if (!r.ok) throw new Error("init failed: " + r.status + " " + await r.text());
    const init = await r.json();

    setStatus("3/5 Uploading " + payload.filename + " (" + (payload.blob.size/1048576).toFixed(2) + " MB)...");
    r = await fetch(init.upload_url + "?token=" + encodeURIComponent(init.upload_token), {
      method: "PUT",
      headers: { "Content-Type": "application/octet-stream" },
      body: payload.blob
    });
    if (!r.ok) throw new Error("upload failed: " + r.status + " " + await r.text());

    setStatus("4/5 Starting processing...");
    // agent_id/pack_id/title/retention come from the link; the server overrides
    // whatever is sent here, so placeholders are fine.
    r = await fetch("/api/packs/build-from-upload-ref", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders },
      body: JSON.stringify({ upload_id: init.upload_id, agent_id: "link", pack_id: "link", title: "link" })
    });
    if (!r.ok) throw new Error("build-ref failed: " + r.status + " " + await r.text());
    const ref = await r.json();

    setStatus("5/5 Processing... (job " + ref.job_id + ")");
    let res = null;
    for (let i = 0; i < 150; i++) {
      await sleep(2000);
      const jr = await fetch("/api/upload-links/status?token=" + encodeURIComponent(TOKEN));
      if (!jr.ok) throw new Error("status poll failed: " + jr.status);
      res = await jr.json();
      if (res.status === "COMPLETED" || res.status === "FAILED" || res.status === "EXPIRED") break;
      setStatus("5/5 Processing... status=" + res.status);
    }
    if (!res || res.status !== "COMPLETED") {
      throw new Error("Processing did not complete: " + (res ? res.status : "timeout"));
    }

    setStatus("Done!", "ok");
    renderResult(res);
  } catch (e) {
    setStatus(String(e.message || e), "err");
  } finally {
    $("go").disabled = false;
  }
}

function renderResult(res) {
  const el = $("result");
  let html = "<div><b>Pack:</b> <code>" + res.pack_id + "</code></div>";
  html += "<div><b>Files merged:</b> " + collected.length + "</div>";
  html += "<div><b>Entries:</b> " + (res.entry_count || 0) + "</div>";
  html += "<div><b>Input type:</b> " + (res.input_type || "file") + "</div>";
  const unsup = res.unsupported_files || [];
  if (unsup.length) {
    html += "<div class='err'>Skipped (unsupported): " + unsup.map(u => u.filename).join(", ") + "</div>";
  }
  if (res.download_url) {
    const shareUrl = res.download_url.startsWith("http")
      ? res.download_url : (location.origin + res.download_url);
    html += "<div class='share'><b>Share link (reuse this pack):</b>"
         + "<code class='u' id='shareUrl'>" + shareUrl + "</code>"
         + "<button id='copyBtn' type='button'>Copy share link</button>"
         + "<div style='font-size:.75rem;color:var(--muted);margin-top:6px'>"
         + "This link is also returned to the assistant automatically. Paste it into "
         + "GPT's <code>importPackByRef</code> to reuse the pack.</div></div>";
    html += "<a class='dl' href='" + shareUrl + "' target='_blank' rel='noopener'>Download pack</a>";
  }
  el.innerHTML = html;
  const copy = $("copyBtn");
  if (copy) copy.onclick = async () => {
    try { await navigator.clipboard.writeText($("shareUrl").textContent); copy.textContent = "Copied!"; }
    catch (e) { copy.textContent = "Copy failed"; }
  };
}
$("go").onclick = run;
"""

_UPLOAD_PAGE = (
    _HEAD + _UPLOAD_BODY + "\n<script>" + _COLLECT_JS + _UPLOAD_RUN_JS + "</script>\n</body>\n</html>"
)

# --- Import page: register pre-built .smp.json packs into the catalog -------
# Unlike /upload (which merges raw source files into ONE new pack), this page
# imports already-built packs. Each selected .smp.json becomes a SEPARATE pack,
# so the files are uploaded and imported one at a time (never zipped/merged).
_IMPORT_BODY = """<body>
<div class="wrap">
  <h1>Safe Memory &mdash; Import Packs</h1>
  <p class="sub">Upload one or more <b>pre-built</b> <code>.smp.json</code> packs to register
  them in an agent's catalog so they become queryable by <code>pack_id</code>. Each file is
  imported as a <b>separate</b> pack (they are <b>not</b> merged). Use this for packs that were
  built elsewhere and cannot be attached through GPT. Bytes go straight to the server.</p>

  <div class="card">
    <label>API key (X-Safe-Memory-Key)</label>
    <input id="apiKey" type="password" placeholder="your Safe Memory API key" />
    <label>agent_id (packs are imported into this agent's vault)</label>
    <input id="agentId" value="tax-agent" />
    <label>.smp.json pack files</label>
    <div id="drop">Drop .smp.json files here, or use the button below<br><small id="fname"></small></div>
    <div class="picks">
      <button id="pickFiles" type="button">Choose .smp.json files&hellip;</button>
    </div>
    <div id="flist"></div>
    <input id="file" type="file" multiple accept=".smp.json,.json" style="display:none" />
    <button id="go">Import Packs</button>
    <div id="status"></div>
    <div id="result" class="result"></div>
  </div>
</div>"""

_IMPORT_RUN_JS = """
const $ = (id) => document.getElementById(id);
const drop = $("drop"), fileInput = $("file");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let picked = [];  // array of File

function setStatus(msg, cls) {
  const el = $("status"); el.textContent = msg; el.className = cls || "";
}
function renderList() {
  $("fname").textContent = picked.length ? (picked.length + " file(s) selected") : "";
  $("flist").innerHTML = picked.map((f) => f.name).join("<br>");
}
function addFiles(files) {
  for (const f of files) {
    const lower = (f.name || "").toLowerCase();
    if (lower.endsWith(".json")) picked.push(f);
  }
  renderList();
}
$("pickFiles").onclick = () => fileInput.click();
fileInput.onchange = (e) => addFiles(e.target.files);
drop.onclick = () => fileInput.click();
["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => {
  e.preventDefault(); drop.classList.add("hot");
}));
["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => {
  e.preventDefault(); drop.classList.remove("hot");
}));
drop.addEventListener("drop", (e) => { if (e.dataTransfer) addFiles(e.dataTransfer.files); });

async function importOne(file, authHeaders, agentId) {
  let r = await fetch("/api/uploads/init", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders },
    body: JSON.stringify({ filename: file.name, content_type: "application/json", size: file.size })
  });
  if (!r.ok) throw new Error("init failed: " + r.status + " " + await r.text());
  const init = await r.json();

  r = await fetch(init.upload_url + "?token=" + encodeURIComponent(init.upload_token), {
    method: "PUT",
    headers: { "Content-Type": "application/octet-stream" },
    body: file
  });
  if (!r.ok) throw new Error("upload failed: " + r.status + " " + await r.text());

  r = await fetch("/api/packs/import-from-upload-ref", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders },
    body: JSON.stringify({ upload_id: init.upload_id, agent_id: agentId })
  });
  if (!r.ok) throw new Error("import failed: " + r.status + " " + await r.text());
  return await r.json();
}

async function run() {
  const key = $("apiKey").value.trim();
  const agentId = $("agentId").value.trim();
  if (!picked.length) { setStatus("Please choose one or more .smp.json files.", "err"); return; }
  if (!agentId) { setStatus("Please enter an agent_id.", "err"); return; }
  const authHeaders = key ? { "X-Safe-Memory-Key": key } : {};
  $("go").disabled = true; $("result").innerHTML = "";
  const results = [];
  try {
    for (let i = 0; i < picked.length; i++) {
      const f = picked[i];
      setStatus("Importing " + (i + 1) + "/" + picked.length + ": " + f.name + " ...");
      try {
        const res = await importOne(f, authHeaders, agentId);
        results.push({ name: f.name, ok: true, res });
      } catch (e) {
        results.push({ name: f.name, ok: false, error: String(e.message || e) });
      }
    }
    const okCount = results.filter((x) => x.ok).length;
    setStatus("Done: " + okCount + "/" + results.length + " imported.", okCount === results.length ? "ok" : "err");
    renderResults(results, agentId);
  } finally {
    $("go").disabled = false;
  }
}

function renderResults(results, agentId) {
  const el = $("result");
  let html = "";
  for (const r of results) {
    if (r.ok) {
      const j = r.res;
      const counts = j.classification_summary || {};
      html += "<div class='share'>"
           + "<div class='ok'><b>&#10003; " + r.name + "</b></div>"
           + "<div><b>pack_id:</b> <code>" + j.pack_id + "</code></div>"
           + "<div><b>entries:</b> " + j.entry_count + "</div>"
           + "<div><b>verified:</b> " + (j.verified ? "yes" : "no") + "</div>"
           + "<div><b>classifications:</b> " + JSON.stringify(counts) + "</div>";
      if ((j.warnings || []).length) {
        html += "<div class='err'>warnings: " + j.warnings.join("; ") + "</div>";
      }
      html += "<div style='font-size:.75rem;color:var(--muted);margin-top:6px'>"
           + "Query it via <code>queryMemoryPack</code> with agent_id=<code>" + agentId
           + "</code> and pack_id=<code>" + j.pack_id + "</code>.</div>"
           + "</div>";
    } else {
      html += "<div class='share'><div class='err'><b>&#10007; " + r.name + "</b></div>"
           + "<div class='err'>" + r.error + "</div></div>";
    }
  }
  el.innerHTML = html;
}
$("go").onclick = run;
"""

_IMPORT_PAGE = (
    _HEAD + _IMPORT_BODY + "\n<script>" + _IMPORT_RUN_JS + "</script>\n</body>\n</html>"
)

_UPLOAD_TOKEN_PAGE = (
    _HEAD + _TOKEN_BODY + "\n<script>" + _COLLECT_JS + _TOKEN_RUN_JS + "</script>\n</body>\n</html>"
)

# --- Keyless one-time IMPORT page (mode="import" links) ---------------------
# Like /import but keyless: the user drops finished .smp.json packs and they are
# registered into the link's PRIVATE, temporary namespace (server-derived), so
# the user can query only their own packs for the life of the link. The
# ephemeral agent_id is never shown here; the assistant learns it via
# getUploadLinkResult.
_TOKEN_IMPORT_BODY = """<body>
<div class="wrap">
  <h1>Safe Memory &mdash; Import Your Packs</h1>
  <p class="sub">Upload one or more <b>pre-built</b> <code>.smp.json</code> packs. They are
  imported into a <b>private, temporary</b> space just for this link and are automatically
  deleted when the link expires. <b>No login or API key needed.</b> Each file becomes a
  <b>separate</b> pack. Bytes go straight to the server, never through GPT/Claude.</p>

  <div class="card">
    <label>.smp.json pack files</label>
    <div id="drop">Drop .smp.json files here, or use the button below<br><small id="fname"></small></div>
    <div class="picks">
      <button id="pickFiles" type="button">Choose .smp.json files&hellip;</button>
    </div>
    <div id="flist"></div>
    <input id="file" type="file" multiple accept=".smp.json,.json" style="display:none" />
    <button id="go">Import Packs</button>
    <div id="status"></div>
    <div id="result" class="result"></div>
  </div>
</div>"""

_TOKEN_IMPORT_RUN_JS = """
// The one-time token is the last path segment (/u/{token}); it authorizes
// staging + import into this link's private namespace. No API key is present.
const TOKEN = decodeURIComponent((location.pathname.split("/").filter(Boolean).pop()) || "");
const $ = (id) => document.getElementById(id);
const drop = $("drop"), fileInput = $("file");
let picked = [];  // array of File

function setStatus(msg, cls) {
  const el = $("status"); el.textContent = msg; el.className = cls || "";
}
function renderList() {
  $("fname").textContent = picked.length ? (picked.length + " file(s) selected") : "";
  $("flist").innerHTML = picked.map((f) => f.name).join("<br>");
}
function addFiles(files) {
  for (const f of files) {
    const lower = (f.name || "").toLowerCase();
    if (lower.endsWith(".json")) picked.push(f);
  }
  renderList();
}
$("pickFiles").onclick = () => fileInput.click();
fileInput.onchange = (e) => addFiles(e.target.files);
drop.onclick = () => fileInput.click();
["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => {
  e.preventDefault(); drop.classList.add("hot");
}));
["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => {
  e.preventDefault(); drop.classList.remove("hot");
}));
drop.addEventListener("drop", (e) => { if (e.dataTransfer) addFiles(e.dataTransfer.files); });

async function importOne(file) {
  const authHeaders = { "X-Upload-Token": TOKEN };
  let r = await fetch("/api/uploads/init", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders },
    body: JSON.stringify({ filename: file.name, content_type: "application/json", size: file.size })
  });
  if (!r.ok) throw new Error("init failed: " + r.status + " " + await r.text());
  const init = await r.json();

  r = await fetch(init.upload_url + "?token=" + encodeURIComponent(init.upload_token), {
    method: "PUT",
    headers: { "Content-Type": "application/octet-stream" },
    body: file
  });
  if (!r.ok) throw new Error("upload failed: " + r.status + " " + await r.text());

  // agent_id is a placeholder; the server overrides it with this link's
  // private namespace in token mode.
  r = await fetch("/api/packs/import-from-upload-ref", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders },
    body: JSON.stringify({ upload_id: init.upload_id, agent_id: "import" })
  });
  if (!r.ok) throw new Error("import failed: " + r.status + " " + await r.text());
  return await r.json();
}

async function run() {
  if (!picked.length) { setStatus("Please choose one or more .smp.json files.", "err"); return; }
  $("go").disabled = true; $("result").innerHTML = "";
  const results = [];
  try {
    for (let i = 0; i < picked.length; i++) {
      const f = picked[i];
      setStatus("Importing " + (i + 1) + "/" + picked.length + ": " + f.name + " ...");
      try {
        const res = await importOne(f);
        results.push({ name: f.name, ok: true, res });
      } catch (e) {
        results.push({ name: f.name, ok: false, error: String(e.message || e) });
      }
    }
    const okCount = results.filter((x) => x.ok).length;
    setStatus("Done: " + okCount + "/" + results.length + " imported.", okCount === results.length ? "ok" : "err");
    renderResults(results);
  } finally {
    $("go").disabled = false;
  }
}

function renderResults(results) {
  const el = $("result");
  let html = "";
  const okCount = results.filter((x) => x.ok).length;
  if (okCount) {
    html += "<div class='share'><div class='ok'><b>&#10003; " + okCount
         + " pack(s) imported into your private space.</b></div>"
         + "<div style='font-size:.8rem;color:var(--muted);margin-top:6px'>"
         + "Return to your assistant &mdash; it can now query these packs. They are "
         + "automatically deleted when this link expires.</div></div>";
  }
  for (const r of results) {
    if (r.ok) {
      const j = r.res;
      const counts = j.classification_summary || {};
      html += "<div class='share'>"
           + "<div class='ok'><b>&#10003; " + r.name + "</b></div>"
           + "<div><b>pack_id:</b> <code>" + j.pack_id + "</code></div>"
           + "<div><b>entries:</b> " + j.entry_count + "</div>"
           + "<div><b>verified:</b> " + (j.verified ? "yes" : "no") + "</div>"
           + "<div><b>classifications:</b> " + JSON.stringify(counts) + "</div>";
      if ((j.warnings || []).length) {
        html += "<div class='err'>warnings: " + j.warnings.join("; ") + "</div>";
      }
      html += "</div>";
    } else {
      html += "<div class='share'><div class='err'><b>&#10007; " + r.name + "</b></div>"
           + "<div class='err'>" + r.error + "</div></div>";
    }
  }
  el.innerHTML = html;
}
$("go").onclick = run;
"""

_UPLOAD_TOKEN_IMPORT_PAGE = (
    _HEAD + _TOKEN_IMPORT_BODY + "\n<script>" + _TOKEN_IMPORT_RUN_JS + "</script>\n</body>\n</html>"
)

_TOKEN_ERROR_PAGE = (
    _HEAD
    + """<body>
<div class="wrap">
  <h1>Safe Memory &mdash; Link Unavailable</h1>
  <div class="card">
    <p class="sub">This one-time upload link is invalid, has expired, or has already
    been used. One-time links are single-use and time-limited for security.</p>
    <p class="sub">Please ask the assistant to generate a fresh upload link and try again.</p>
  </div>
</div>
</body>
</html>"""
)


@router.get("/upload", response_class=HTMLResponse, include_in_schema=False)
def upload_page() -> HTMLResponse:
    """Serve the self-contained browser upload page (no auth; key in the form)."""
    return HTMLResponse(content=_UPLOAD_PAGE)


@router.get("/import", response_class=HTMLResponse, include_in_schema=False)
def import_page() -> HTMLResponse:
    """Serve the browser page for importing pre-built .smp.json packs (key in the form)."""
    return HTMLResponse(content=_IMPORT_PAGE)


@router.get("/u/{token}", response_class=HTMLResponse, include_in_schema=False)
def one_time_upload_page(token: str) -> HTMLResponse:
    """Serve the keyless, single-use upload page for a one-time link token."""
    claim = upload_links.find_claim_by_token((token or "").strip())
    if claim is None:
        return HTMLResponse(content=_TOKEN_ERROR_PAGE, status_code=404)
    # Import-mode links serve the "upload finished .smp.json packs" UI; the
    # default (build) mode serves the "drop raw files, build a pack" UI.
    if (getattr(claim, "mode", "build") or "build") == "import":
        return HTMLResponse(content=_UPLOAD_TOKEN_IMPORT_PAGE)
    return HTMLResponse(content=_UPLOAD_TOKEN_PAGE)
