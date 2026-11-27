"use strict";
// RAIDPool – Reiter „Duplikate“

const dz = {
  status: null, data: null, key: "", offset: 0, search: "", showIgnored: false,
  sel: new Map(),          // "pool\npath" -> {hash, size}
  cfgDraft: null, settingsOpen: false,
};
const DAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"];
const selKey = (f) => f.pool + "\n" + f.path;
window.dupesRunning = () => !!(dz.status && dz.status.progress && dz.status.progress.running);

function fileIcon(name) {
  const ext = (name.split(".").pop() || "").toLowerCase();
  const map = [
    [["mkv", "mp4", "avi", "mov", "m2ts", "ts", "wmv", "webm", "m4v"], "🎬"],
    [["jpg", "jpeg", "png", "gif", "heic", "webp", "raw", "cr2", "nef", "arw", "dng", "tif", "tiff"], "🖼️"],
    [["mp3", "flac", "wav", "m4a", "aac", "ogg", "opus", "wma"], "🎵"],
    [["pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "odt", "rtf", "epub"], "📄"],
    [["zip", "rar", "7z", "tar", "gz", "xz", "bz2"], "📦"],
    [["iso", "img", "vhd", "vhdx", "vmdk", "qcow2"], "💿"],
    [["exe", "msi", "dll", "apk", "deb", "dmg"], "⚙️"],
  ];
  for (const [exts, icon] of map) if (exts.includes(ext)) return icon;
  return "📁";
}

// Ordnerpfade vergleichen: gemeinsamer Anfang grau, Unterschied fett
function pathParts(files) {
  const split = files.map((f) => [f.pool].concat(f.path.split("/").slice(0, -1)));
  let common = 0;
  while (split.every((s) => s.length > common && s[common] === split[0][common])) common++;
  return split.map((s, i) => {
    const pool = esc(files[i].pool_name) + " › ";
    const dirs = s.slice(1);
    const c = Math.max(0, common - 1);
    const pre = dirs.slice(0, c).join("/");
    const rest = dirs.slice(c).join("/");
    if (!dirs.length) return pool + "<b>/</b>";
    return pool + esc(pre) + (pre && rest ? "/" : "") + (rest ? "<b>" + esc(rest) + "</b>" : "");
  });
}

async function loadDupes(force) {
  dz.status = await api("dupes/status");
  const last = dz.status.last || {};
  const key = [last.id, last.status, dz.search, dz.showIgnored, dz.offset].join("|");
  if (force || key !== dz.key || !dz.data) {
    const q = `offset=0&limit=${50 + dz.offset}&search=${encodeURIComponent(dz.search)}&ignored=${dz.showIgnored ? 1 : 0}`;
    dz.data = await api("dupes/groups?" + q);
    dz.key = key;
    dz.trash = await api("dupes/trash").catch(() => null);
    dz.ignored = await api("dupes/ignored").catch(() => []);
  }
}

window.renderDupes = async function (force) {
  const el = $("#tab-dupes");
  try { await loadDupes(force); } catch (e) { el.innerHTML = `<div class="card">Fehler: ${esc(e.message)}</div>`; return; }
  const st = dz.status; const p = st.progress || {}; const sum = st.summary; const last = st.last;
  const running = !!p.running;
  if (!dz.cfgDraft || !dz.settingsOpen) dz.cfgDraft = JSON.parse(JSON.stringify(st.config));
  const modeText = { manual: "Manuell", schedule: "Nach Zeitplan", auto: "Automatisch im Leerlauf" }[st.config.mode];
  const pct = p.total ? Math.round((100 * p.done) / p.total) : 0;

  el.innerHTML = `
  <div class="card hero">
    <div>
      <div class="big">${fmtBytes(sum.wasted)}<small>durch Duplikate belegt</small></div>
    </div>
    <div>
      <div class="stats" style="margin-top:0">
        <div class="stat"><b>${fmtNum(sum.groups)}</b><span>Gruppen</span></div>
        <div class="stat"><b>${fmtNum(sum.files)}</b><span>Dateien</span></div>
        <div class="stat"><b>${fmtNum(sum.ignored)}</b><span>ignoriert</span></div>
      </div>
      <div class="row spread">
        <div class="small muted">${last ? `Letzte Suche: ${fmtTime(last.finished || last.started)} · ${esc(last.trigger)} · ${esc(last.status)}${last.scope ? `<br>Nur in: ${esc(last.scope)}` : ""}` : "Noch nie gesucht"}<br>Modus: ${modeText}</div>
        ${running ? `<button class="btn danger" onclick="act(()=>api('dupes/stop',{}),'Wird abgebrochen …')">Abbrechen</button>`
                  : `<div class="row"><button class="btn" onclick="scopeDialog()" title="Nur bestimmte Ordner durchsuchen">Ordner durchsuchen …</button>
                     <button class="btn primary" onclick="act(()=>api('dupes/start',{}),'Duplikatsuche gestartet')">Jetzt suchen</button></div>`}
      </div>
    </div>
  </div>

  ${running ? `<div class="card">
    <div class="row spread"><div class="phase"><div class="spinner"></div><b>${esc(p.phase)}</b>
      ${p.paused ? '<span class="badge warn">pausiert – Rechner beschäftigt</span>' : ""}</div>
      <span class="small muted">${esc(p.trigger)}</span></div>
    <div class="progress big"><div style="width:${pct}%"></div></div>
    <div class="small muted" style="margin-top:6px">${fmtNum(p.done)} / ${fmtNum(p.total)} Dateien · ${fmtBytes(p.read_bytes)} gelesen · ${fmtNum(p.groups)} Gruppen bisher
    · CPU ${st.idle.cpu} % · fremde Plattenlast ${st.idle.io_mb_s} MB/s</div></div>` : ""}

  ${settingsCard(st)}

  <div class="toolbar">
    <input type="text" id="dz-search" placeholder="Suchen nach Name oder Ordner …" value="${esc(dz.search)}">
    <label class="row small"><input type="checkbox" id="dz-ign" ${dz.showIgnored ? "checked" : ""}> Ignorierte anzeigen</label>
    ${dz.showIgnored ? "" : `<div class="row small"><span class="muted">Automatisch markieren:</span>
      <button class="btn" onclick="bulkSelect('newest')" title="In allen Gruppen die neueste Datei behalten">Neueste behalten</button>
      <button class="btn" onclick="bulkSelect('oldest')" title="In allen Gruppen die älteste Datei behalten">Älteste behalten</button>
      <button class="btn" onclick="bulkSelect('shortest')" title="Datei mit dem kürzesten Pfad behalten">Kürzesten Pfad behalten</button>
      <button class="btn" onclick="bulkSelect('none')">Keine</button></div>`}
  </div>

  ${ignoredCard()}

  ${dz.data.groups.length ? dz.data.groups.map(groupCard).join("") :
    `<div class="card empty">${dz.showIgnored ? "Keine ignorierten Duplikate." : last ? "🎉 Keine Duplikate gefunden." : "Noch keine Suche durchgeführt."}</div>`}
  ${dz.data.total > dz.data.groups.length ? `<div class="row" style="justify-content:center;margin-bottom:80px"><button class="btn" onclick="moreDupes()">Weitere ${Math.min(50, dz.data.total - dz.data.groups.length)} von ${fmtNum(dz.data.total - dz.data.groups.length)} laden</button></div>` : '<div style="height:70px"></div>'}
  ${dz.trash && dz.trash.files ? `<div class="small muted" style="text-align:center;margin-bottom:90px">Papierkorb: ${fmtNum(dz.trash.files)} Dateien, ${fmtBytes(dz.trash.bytes)} – wird nach ${st.config.trash_days} Tagen automatisch geleert
     · <a href="#" onclick="purgeTrash();return false">jetzt leeren</a></div>` : ""}
  `;
  const s = $("#dz-search");
  s.onkeydown = (e) => { if (e.key === "Enter") { dz.search = s.value; dz.offset = 0; renderDupes(true); } };
  s.onblur = () => { if (s.value !== dz.search) { dz.search = s.value; dz.offset = 0; renderDupes(true); } };
  $("#dz-ign").onchange = (e) => { dz.showIgnored = e.target.checked; dz.offset = 0; dz.sel.clear(); renderDupes(true); };
  const det = $("#dz-settings");
  if (det) det.ontoggle = () => { dz.settingsOpen = det.open; };
  const ig = $("#dz-ignored");
  if (ig) ig.ontoggle = () => { dz.ignoredOpen = ig.open; };
  renderSelbar();
};

function settingsCard(st) {
  const c = dz.cfgDraft;
  return `<details class="card settings" id="dz-settings" ${dz.settingsOpen ? "open" : ""}><summary>Einstellungen der Duplikatsuche</summary>
    <div class="field"><label>Ausführung</label>
      <div class="seg">${[["manual", "Manuell"], ["schedule", "Zeitplan"], ["auto", "Automatisch im Leerlauf"]]
        .map(([m, l]) => `<button class="${c.mode === m ? "active" : ""}" onclick="dzSet('mode','${m}')">${l}</button>`).join("")}</div></div>
    ${c.mode === "schedule" ? `<div class="field"><label>Tage und Uhrzeit</label><div class="row">
      <div class="chips">${DAYS.map((d, i) => `<button class="${c.days.includes(i) ? "on" : ""}" onclick="dzDay(${i})">${d}</button>`).join("")}</div>
      <input type="time" value="${esc(c.time)}" onchange="dzSet('time', this.value)"></div></div>` : ""}
    ${c.mode === "auto" ? `<div class="muted small">Startet nur, wenn der Rechner seit einigen Minuten im Leerlauf ist, und pausiert sofort, sobald er wieder etwas zu tun bekommt.</div>
      <div class="settings-grid">
      <div class="field"><label>Höchstens alle … Stunden</label><input type="number" min="1" max="720" value="${c.auto_interval_hours}" onchange="dzSet('auto_interval_hours', +this.value)"></div>
      <div class="field"><label>Leerlauf seit … Minuten</label><input type="number" min="1" max="120" value="${c.idle_minutes}" onchange="dzSet('idle_minutes', +this.value)"></div>
      <div class="field"><label>Leerlauf = CPU unter … %</label><input type="number" min="5" max="90" value="${c.idle_cpu_percent}" onchange="dzSet('idle_cpu_percent', +this.value)"></div>
      <div class="field"><label>… und Plattenlast unter … MB/s</label><input type="number" min="0" max="500" step="0.5" value="${c.idle_io_mb_s}" onchange="dzSet('idle_io_mb_s', +this.value)"></div>
      </div>` : ""}
    <div class="settings-grid">
      <div class="field"><label>Dateien ab … KB</label><input type="number" min="1" value="${c.min_size_kb}" onchange="dzSet('min_size_kb', +this.value)"></div>
      <div class="field"><label>Lesegeschwindigkeit max. … MB/s</label><input type="number" min="5" max="2000" value="${c.max_mb_s}" onchange="dzSet('max_mb_s', +this.value)"></div>
      <div class="field"><label>Papierkorb aufbewahren … Tage</label><input type="number" min="0" max="365" value="${c.trash_days}" onchange="dzSet('trash_days', +this.value)"></div>
    </div>
    <label class="row small"><input type="checkbox" ${c.refresh_index ? "checked" : ""} onchange="dzSet('refresh_index', this.checked)"> Dateiliste vor der Suche aktualisieren</label>
    <label class="row small"><input type="checkbox" ${c.use_trash ? "checked" : ""} onchange="dzSet('use_trash', this.checked)"> Gelöschte Dateien zuerst in den Papierkorb verschieben</label>
    <div class="field"><label>Zusätzlich durchsuchen <span class="muted small">(Platten oder Netzwerkfreigaben außerhalb der Pools)</span></label>
      <div class="excl">${(st.sources || []).length ? st.sources.map((s) => `<span class="chip">${s.network ? "🌐" : "💽"} ${esc(s.name)}${s.mounted ? "" : ' <span class="badge bad">getrennt</span>'}
        <button type="button" title="Entfernen" onclick='removeSource(${js(s.uuid)})'>✕</button></span>`).join("") : '<span class="muted small">Nur die Pools werden durchsucht.</span>'}</div>
      <div><button type="button" class="btn" onclick="pickSource()">+ Quelle hinzufügen …</button></div></div>
    <div class="field"><label>Ausgeschlossene Ordner <span class="muted small">(werden sofort gespeichert)</span></label>
      <div class="excl">${st.config.excludes.length ? st.config.excludes.map((e) => `<span class="chip">📂 ${esc(exclLabel(e))}
        <button type="button" title="Entfernen" onclick='removeExclude(${js(e)})'>✕</button></span>`).join("") : '<span class="muted small">Keine – alle Ordner werden durchsucht.</span>'}</div>
      <div><button type="button" class="btn" onclick="pickExclude()">+ Ordner auswählen …</button></div></div>
    <div class="row" style="justify-content:flex-end"><button class="btn primary" onclick="saveDzCfg()">Speichern</button></div>
  </details>`;
}
window.dzSet = (k, v) => { dz.cfgDraft[k] = v; if (k === "mode") renderDupes(); };
window.dzDay = (i) => { const d = dz.cfgDraft.days; const at = d.indexOf(i); at >= 0 ? d.splice(at, 1) : d.push(i); d.sort(); renderDupes(); };
window.saveDzCfg = () => act(async () => { await api("dupes/config", dz.cfgDraft); dz.settingsOpen = false; }, "Einstellungen gespeichert");

function groupCard(g) {
  const name = g.files[0].path.split("/").pop();
  const paths = pathParts(g.files);
  const marked = g.files.filter((f) => dz.sel.has(selKey(f))).length;
  return `<div class="card dgroup">
    <div class="dgroup-head">
      <div class="ficon">${fileIcon(name)}</div>
      <div class="dgroup-title"><b title="${esc(name)}">${g.same_name ? esc(name) : esc(name) + ` <span class="muted small">+ ${g.count - 1} andere Namen</span>`}</b>
        <div class="row small muted">${fmtBytes(g.size)} · ${g.count} Kopien · <span style="color:var(--seg-dup)">${fmtBytes(g.wasted)} doppelt</span>
          ${g.same_name ? '<span class="badge">gleicher Name</span>' : '<span class="badge info">unterschiedliche Namen</span>'}
          ${g.ignored ? `<span class="badge warn">${g.ignored} ignoriert</span>` : ""}
          ${marked ? `<span class="badge bad">${marked} zum Löschen</span>` : ""}</div></div>
      <div class="row">
        ${g.ignored ? `<button class="btn" onclick='ignoreGroup(${js(g.hash)},"unignore")'>Nicht mehr ignorieren</button>`
          : `<button class="btn" onclick='ignoreGroup(${js(g.hash)},"once")' title="Bis zur nächsten Suche ausblenden">Einmal ignorieren</button>
             <button class="btn" onclick='ignoreGroup(${js(g.hash)},"forever")' title="Diese Dateien nie wieder anzeigen">Immer ignorieren</button>`}
      </div>
    </div>
    ${g.files.map((f, i) => {
      const on = dz.sel.has(selKey(f));
      return `<div class="dfile ${on ? "del" : ""}" onclick='toggleDel(${js(g.hash)}, ${i})'>
        <input type="checkbox" ${on ? "checked" : ""} ${g.ignored ? "disabled" : ""} onclick="event.stopPropagation(); toggleDel(${js(g.hash)}, ${i})">
        <div class="fname">${esc(f.path.split("/").pop())}</div>
        <div class="small ${on ? "" : "keep"}" style="text-align:right;white-space:nowrap">${on ? "wird gelöscht" : "✓ bleibt"} · ${fmtTime(f.mtime).split(",")[0]}</div>
        <div class="fpath">📂 ${paths[i]}</div>
      </div>`;
    }).join("")}
  </div>`;
}

function groupByHash(h) { return dz.data.groups.find((g) => g.hash === h); }
window.toggleDel = (h, i) => {
  const g = groupByHash(h); if (!g || g.ignored) return;
  const f = g.files[i]; const k = selKey(f);
  if (dz.sel.has(k)) dz.sel.delete(k);
  else {
    const others = g.files.filter((x) => x !== f && !dz.sel.has(selKey(x))).length;
    if (!others) { toast("Mindestens eine Datei pro Gruppe muss bleiben"); return; }
    dz.sel.set(k, { hash: h, size: g.size, pool: f.pool, path: f.path });
  }
  renderDupes();
};
window.bulkSelect = (how) => {
  dz.sel.clear();
  if (how !== "none") for (const g of dz.data.groups) {
    if (g.ignored) continue;
    let keep;
    if (how === "newest") keep = g.files.reduce((a, b) => (b.mtime > a.mtime ? b : a));
    else if (how === "oldest") keep = g.files.reduce((a, b) => (b.mtime < a.mtime ? b : a));
    else keep = g.files.reduce((a, b) => (b.path.length < a.path.length ? b : a));
    for (const f of g.files) if (f !== keep) dz.sel.set(selKey(f), { hash: g.hash, size: g.size, pool: f.pool, path: f.path });
  }
  renderDupes();
};
window.ignoreGroup = (h, kind) => act(async () => {
  await api("dupes/ignore", { hashes: [h], kind });
  for (const [k, v] of dz.sel) if (v.hash === h) dz.sel.delete(k);
  dz.key = "";
}, { once: "Bis zur nächsten Suche ausgeblendet", forever: "Wird dauerhaft ignoriert", unignore: "Wird wieder angezeigt" }[kind]);
window.moreDupes = () => { dz.offset += 50; renderDupes(true); };
window.purgeTrash = () => {
  if (!confirm("Papierkorb jetzt endgültig leeren?")) return;
  act(async () => { await api("dupes/purge", { days: 0 }); dz.key = ""; }, "Papierkorb geleert");
};

function renderSelbar() {
  const bar = $("#selbar");
  if (state.tab !== "dupes" || !dz.sel.size) { bar.hidden = true; return; }
  const bytes = [...dz.sel.values()].reduce((a, v) => a + v.size, 0);
  const ro = dz.status.read_only;
  bar.hidden = false;
  bar.innerHTML = `<div class="grow"><b>${fmtNum(dz.sel.size)} Dateien</b> ausgewählt · <span style="color:var(--ok)">${fmtBytes(bytes)} werden frei</span>
      ${ro ? '<br><span class="small muted">Schreibschutz aktiv – Löschen erst nach Ausschalten von read_only möglich</span>' : ""}</div>
    <button class="btn" onclick="dzClear()">Auswahl aufheben</button>
    <button class="btn primary" style="background:var(--bad);border-color:var(--bad)" ${ro ? "disabled" : ""} onclick="dzDelete()">Löschen</button>`;
}
window.dzClear = () => { dz.sel.clear(); renderDupes(); };
window.dzDelete = () => {
  const items = [...dz.sel.values()];
  const bytes = items.reduce((a, v) => a + v.size, 0);
  const trash = dz.status.config.use_trash;
  $("#dlg-body").innerHTML = `<h2>${fmtNum(items.length)} Dateien löschen?</h2>
    <p>${fmtBytes(bytes)} werden frei. Jede Datei wird vorher noch einmal <b>Byte für Byte</b> mit einer verbleibenden Kopie verglichen.</p>
    <p class="${trash ? "muted" : ""}">${trash ? `Die Dateien kommen zuerst in den Papierkorb und werden nach ${dz.status.config.trash_days} Tagen endgültig gelöscht.`
      : "<b style='color:var(--bad)'>Der Papierkorb ist ausgeschaltet – die Dateien werden sofort endgültig gelöscht.</b>"}</p>
    <div style="max-height:180px;overflow:auto" class="mono small">${items.slice(0, 200).map((i) => esc(i.path)).join("<br>")}${items.length > 200 ? "<br>…" : ""}</div>
    <div class="row" style="justify-content:flex-end;margin-top:14px"><button class="btn" value="cancel">Abbrechen</button>
    <button class="btn primary" id="dz-ok" style="background:var(--bad);border-color:var(--bad)">Löschen</button></div>`;
  $("#dz-ok").onclick = async (ev) => {
    ev.preventDefault(); $("#dlg").close();
    try {
      const r = await api("dupes/delete", { items: items.map(({ pool, path }) => ({ pool, path })) });
      dz.sel.clear(); dz.key = "";
      toast(`${fmtNum(r.deleted)} Dateien gelöscht, ${fmtBytes(r.freed)} frei` + (r.errors.length ? ` – ${r.errors.length} übersprungen` : ""));
      if (r.errors.length) console.warn(r.errors);
    } catch (e) { toast("Fehler: " + e.message); }
    renderDupes(true);
  };
  $("#dlg").showModal();
};

// ---------------------------------------------------------------- Ausschlüsse mit Ordnerauswahl
function exclLabel(e) {
  if (!e.includes(":")) return "Alle Pools › " + e;
  const [pid, ...rest] = e.split(":");
  const pool = (state.status.pools.find((p) => p.id === pid) || {}).name || pid;
  return pool + " › " + (rest.join(":") || "/");
}
async function saveExcludes(list) {
  await api("dupes/config", { excludes: list });
  if (dz.cfgDraft) dz.cfgDraft.excludes = list.slice();
}
window.removeExclude = (e) => act(() => saveExcludes(dz.status.config.excludes.filter((x) => x !== e)), "Ordner wird wieder durchsucht");
window.pickExclude = () => openFolderPicker((pool, path) => {
  const v = pool + ":" + path;
  const list = dz.status.config.excludes;
  if (list.includes(v)) { toast("Ordner ist bereits ausgeschlossen"); return; }
  act(() => saveExcludes(list.concat([v])), "Ordner ausgeschlossen");
});

async function openFolderPicker(onPick, pool = null, path = "", opts = {}) {
  const o = Object.assign({ title: "Ordner ausschließen", ok: "Diesen Ordner ausschließen", fixedPool: false, allowRoot: false, markExcluded: true }, opts);
  let data;
  try { data = await api(`browse?pool=${encodeURIComponent(pool || "")}&path=${encodeURIComponent(path)}`); }
  catch (e) { toast("Fehler: " + e.message); return; }
  const parts = path ? path.split("/") : [];
  const crumbs = (o.fixedPool ? [] : [`<a data-p="" data-pool="">Pools</a>`])
    .concat(pool ? [`<a data-pool="${esc(pool)}" data-p="">${esc(data.pool_name)}</a>`] : [])
    .concat(parts.map((x, i) => `<a data-pool="${esc(pool)}" data-p="${esc(parts.slice(0, i + 1).join("/"))}">${esc(x)}</a>`)).join(" / ");
  if (!dz.status) { try { dz.status = await api("dupes/status"); } catch (e) { /* egal */ } }
  const excluded = new Set(dz.status ? dz.status.config.excludes : []);
  $("#dlg-body").innerHTML = `<h2>${esc(o.title)}</h2>
    <div class="crumbs picker-crumbs">${crumbs}</div>
    <div class="picker">${data.folders.length ? data.folders.map((f) => {
      const ex = o.markExcluded && excluded.has(f.pool + ":" + f.path);
      return `<div class="picker-row" data-pool="${esc(f.pool)}" data-p="${esc(f.path)}">
        <span class="picker-name">${pool ? "📁" : "🗄️"} ${esc(f.name)}</span>
        <span class="muted small">${f.files != null ? fmtNum(f.files) + " Dateien · " + fmtBytes(f.bytes) : ""}</span>
        ${ex ? '<span class="badge warn">ausgeschlossen</span>' : "<span>›</span>"}</div>`;
    }).join("") : '<div class="muted small" style="padding:12px">Keine Unterordner.</div>'}</div>
    <div class="row spread" style="margin-top:14px">
      <span class="small muted">${pool ? (o.allowRoot && !path ? "Ausgewählt: <b>" + esc(data.pool_name) + " (ganzer Pool)</b>" : "Ausgewählt: <b>" + esc(data.pool_name + " › " + (path || "/")) + "</b>") : "Pool wählen"}</span>
      <div class="row"><button type="button" class="btn" id="fp-cancel">Abbrechen</button>
      <button type="button" class="btn primary" id="fp-ok" ${pool && (path || o.allowRoot) ? "" : "disabled"}>${esc(o.ok)}</button></div></div>`;
  const dlg = $("#dlg");
  dlg.querySelectorAll("[data-p]").forEach((a) => (a.onclick = () => openFolderPicker(onPick, a.dataset.pool || null, a.dataset.p, o)));
  $("#fp-cancel").onclick = () => (o.onCancel ? o.onCancel() : dlg.close());
  $("#fp-ok").onclick = () => { if (!o.keepOpen) dlg.close(); onPick(pool, path); };
  if (!dlg.open) dlg.showModal();
}

// ---------------------------------------------------------------- Gezielte Suche in Ordnern
dz.scope = dz.scope || { folders: [], within: false };
function scopeLabel(v) {
  const [pool, ...rest] = v.split(":"); const path = rest.join(":");
  const pl = ((state.status && state.status.pools) || []).find((p) => p.id === pool);
  const src = ((dz.status && dz.status.sources) || []).find((x) => x.id === pool);
  return (pl ? pl.name : src ? src.name : pool) + (path ? " › " + path : " (gesamt)");
}
window.scopeDialog = () => {
  const sc = dz.scope;
  $("#dlg-body").innerHTML = `<h2>Bestimmte Ordner durchsuchen</h2>
    <p class="small muted">Wähle einen oder mehrere Ordner. Ausgeschlossene Ordner bleiben ausgeschlossen.</p>
    <div class="picker">${sc.folders.length ? sc.folders.map((f, i) => `<div class="picker-row" style="cursor:default">
        <span class="picker-name">📁 ${esc(scopeLabel(f))}</span>
        <button type="button" class="btn" style="padding:2px 8px;font-size:12px" onclick="scopeRemove(${i})">Entfernen</button></div>`).join("")
      : '<div class="muted small" style="padding:12px">Noch kein Ordner gewählt.</div>'}</div>
    <div style="margin:10px 0"><button type="button" class="btn" onclick="scopeAdd()">+ Ordner hinzufügen</button></div>
    <label class="row small" style="margin:6px 0"><input type="radio" name="sc-mode" value="all" ${sc.within ? "" : "checked"}>
      Mit allen anderen Dateien vergleichen <span class="muted">– findet auch Kopien außerhalb der Ordner</span></label>
    <label class="row small" style="margin:6px 0"><input type="radio" name="sc-mode" value="within" ${sc.within ? "checked" : ""}>
      Nur innerhalb der gewählten Ordner vergleichen</label>
    <div class="row" style="justify-content:flex-end;margin-top:14px">
      <button type="button" class="btn" onclick="$('#dlg').close()">Abbrechen</button>
      <button type="button" class="btn primary" id="sc-go" ${sc.folders.length ? "" : "disabled"}>Suche starten</button></div>`;
  document.querySelectorAll("input[name=sc-mode]").forEach((r) => (r.onchange = () => (sc.within = r.value === "within")));
  $("#sc-go").onclick = () => { $("#dlg").close();
    act(() => api("dupes/start", { folders: sc.folders, within: sc.within }), "Gezielte Duplikatsuche gestartet"); };
  if (!$("#dlg").open) $("#dlg").showModal();
};
window.scopeRemove = (i) => { dz.scope.folders.splice(i, 1); scopeDialog(); };
window.scopeAdd = () => openFolderPicker((pool, path) => {
  const v = pool + ":" + path;
  if (!dz.scope.folders.includes(v)) dz.scope.folders.push(v);
  scopeDialog();
}, null, "", { title: "Ordner für die Suche wählen", ok: "Ordner hinzufügen", allowRoot: true, markExcluded: false,
  keepOpen: true, onCancel: () => scopeDialog() });

// ---------------------------------------------------------------- Dauerhaft ignoriert
function ignoredCard() {
  const list = dz.ignored || [];
  if (!list.length) return "";
  return `<details class="card settings ign" id="dz-ignored" ${dz.ignoredOpen ? "open" : ""}>
    <summary>Dauerhaft ignoriert <span class="badge">${list.length}</span></summary>
    <div class="row" style="justify-content:flex-end;margin-bottom:8px">
      <button type="button" class="btn" onclick='unignore(${js(list.map((x) => x.hash))})'>Alle wieder anzeigen</button></div>
    ${list.map((g) => {
      const name = g.files.length ? g.files[0].path.split("/").pop() : "(Dateien nicht mehr vorhanden)";
      return `<div class="ign-row">
        <div class="ficon">${fileIcon(name)}</div>
        <div style="min-width:0;flex:1">
          <b class="ellip">${esc(name)}</b>
          <div class="small muted">${g.size != null ? fmtBytes(g.size) + " · " : ""}${g.files.length} Dateien · ignoriert seit ${fmtTime(g.created).split(",")[0]}
            ${g.still_present ? "" : ' · <span class="badge">aktuell nicht gefunden</span>'}</div>
          ${g.files.map((f) => `<div class="small mono muted ellip">📂 ${esc(f.pool_name + " › " + f.path)}</div>`).join("")}
        </div>
        <button type="button" class="btn" onclick='unignore(${js([g.hash])})'>Wieder anzeigen</button>
      </div>`;
    }).join("")}
  </details>`;
}
window.unignore = (hashes) => act(async () => { await api("dupes/ignore", { hashes, kind: "unignore" }); dz.key = ""; },
  hashes.length > 1 ? "Alle werden wieder angezeigt" : "Wird wieder angezeigt");

// ---------------------------------------------------------------- Zusätzliche Quellen
async function saveSources(list) {
  await api("dupes/config", { sources: list });
  if (dz.cfgDraft) dz.cfgDraft.sources = list.slice();
}
window.removeSource = (uuid) => act(() => saveSources(dz.status.config.sources.filter((x) => x !== uuid)), "Quelle entfernt");
window.pickSource = async () => {
  state.disks = await api("disks");
  const used = new Set(state.status.pools.flatMap((p) => p.members.map((m) => m.uuid)).concat(dz.status.config.sources));
  const opts = [];
  for (const d of state.disks) {
    if (d.system) continue;
    for (const p of d.partitions) if (p.mountpoint && !used.has(p.uuid)) opts.push({ d, p });
  }
  $("#dlg-body").innerHTML = `<h2>Quelle für die Duplikatsuche</h2>
    <p class="muted small">Die Quelle wird zusätzlich zu den Pools durchsucht – Duplikate werden auch zwischen Pool und Quelle gefunden.</p>
    <div class="picker">${opts.length ? opts.map(({ d, p }, i) => `<div class="picker-row" data-i="${i}">
      <span class="picker-name">${d.network ? "🌐" : "💽"} ${esc(p.label || d.model || p.name)}</span>
      <span class="muted small">${esc(d.path)}${p.usage ? " · " + fmtBytes(p.usage.used) + " belegt" : ""}</span><span>+</span></div>`).join("")
      : '<div class="muted small" style="padding:12px">Keine weiteren verbundenen Platten oder Freigaben. Netzwerkfreigaben fügst du im Reiter „Platten“ hinzu.</div>'}</div>
    <div class="row" style="justify-content:flex-end;margin-top:14px"><button type="button" class="btn" id="src-cancel">Abbrechen</button></div>`;
  const dlg = $("#dlg");
  dlg.querySelectorAll("[data-i]").forEach((r) => (r.onclick = () => {
    dlg.close();
    act(() => saveSources(dz.status.config.sources.concat([opts[+r.dataset.i].p.uuid])), "Quelle hinzugefügt – wird bei der nächsten Suche durchsucht");
  }));
  $("#src-cancel").onclick = () => dlg.close();
  dlg.showModal();
};
