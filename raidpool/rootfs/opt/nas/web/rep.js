"use strict";
// RAIDPool – Duplizierungsdienst (Soll herstellen, Echtzeit, Ausbalancieren, Platte entfernen)

const rp = { cfgOpen: false, logOpen: false };
const ACT_ICON = { dupliziert: "➕", aktualisiert: "🔄", entfernt: "➖", verschoben: "↔️", umkopiert: "📦",
  konflikt: "⚠️", fehler: "❌", info: "ℹ️" };

function repJobLine(r) {
  const j = r.job;
  if (!j || !j.running) return "";
  const pct = j.total ? Math.round((100 * j.done) / j.total) : 0;
  return `<div style="margin-top:10px"><div class="row spread"><div class="phase"><div class="spinner"></div><b>${esc(j.label)}</b>
      <span class="small muted">${esc(j.phase || "")}</span></div>
      <button class="btn" onclick="act(()=>api('rep/cancel',{}),'Wird abgebrochen …')">Abbrechen</button></div>
    ${j.total ? `<div class="progress big"><div style="width:${pct}%"></div></div><div class="small muted">${fmtNum(j.done)} / ${fmtNum(j.total)}</div>` : ""}</div>`;
}
function repCurrent(r) {
  const c = r.current;
  if (!c) return "";
  const pct = c.size ? Math.round((100 * c.done) / c.size) : 0;
  return `<div class="small" style="margin-top:8px">${ACT_ICON[c.action === "duplizieren" ? "dupliziert" : c.action === "verschieben" ? "verschoben" : "aktualisiert"] || "•"}
    ${esc(c.action)}: <span class="mono">${esc(c.path)}</span>${c.to ? " → " + esc(c.to) : ""} · ${pct} % von ${fmtBytes(c.size)}
    <div class="progress"><div style="width:${pct}%"></div></div></div>`;
}

window.repPoolLine = (p) => {
  const r = state.rep;
  if (!r) return "";
  const c = r.config;
  let state_;
  if (r.read_only) state_ = '<span class="badge">Schreibschutz – Duplizierung pausiert</span>';
  else if (!c.enabled) state_ = '<span class="badge">Duplizierung aus</span>';
  else state_ = `<span class="badge ok">Duplizierung aktiv</span>${c.realtime ? (r.watch.active ? ' <span class="badge info">Echtzeit</span>' : ' <span class="badge warn">Echtzeit nicht aktiv</span>') : ""}`;
  return `<div style="margin-top:10px;padding:10px;border-radius:10px;background:var(--bg)">
    <div class="row spread small"><div class="row">${state_}</div>
      <span class="muted">${r.queue ? fmtNum(r.queue) + " in Warteschlange" : "Warteschlange leer"}</span></div>
    ${r.job && r.job.running && (!r.job.kw || true) ? repJobLine(r) : ""}${repCurrent(r)}</div>`;
};

window.repCard = () => {
  const r = state.rep;
  if (!r) return "";
  const c = r.config; const s = r.stats;
  const last = r.job && !r.job.running ? `Letzter Auftrag: ${esc(r.job.label)} – ${esc(r.job.result || "")} (${fmtTime(r.job.finished)})` : "";
  return `<div class="card">
    <div class="row spread"><h2 style="margin:0">Duplizierungsdienst</h2>
      <div class="row">${r.read_only ? '<span class="badge warn">Schreibschutz aktiv – pausiert</span>' : c.enabled ? '<span class="badge ok">aktiv</span>' : '<span class="badge">aus</span>'}
      ${c.realtime && !r.read_only ? (r.watch.active ? `<span class="badge info">Echtzeit · ${r.watch.files ? "alle Dateiänderungen" : fmtNum(r.watch.dirs) + " Ordner überwacht"}</span>` : '<span class="badge warn">Echtzeit nicht aktiv</span>') : ""}</div></div>
    ${r.watch.error ? `<div class="small muted" style="margin-top:6px">⚠️ ${esc(r.watch.error)}</div>` : ""}
    <div class="stats">
      <div class="stat"><b>${fmtNum(r.queue)}</b><span>Warteschlange</span></div>
      <div class="stat"><b>${fmtNum(s.copied_files)}</b><span>Kopien erstellt</span></div>
      <div class="stat"><b>${fmtBytes(s.copied_bytes)}</b><span>kopiert</span></div>
      <div class="stat"><b>${fmtNum(s.removed)}</b><span>Kopien entfernt</span></div>
      <div class="stat"><b style="color:${s.conflicts ? "var(--warn)" : "inherit"}">${fmtNum(s.conflicts)}</b><span>Konflikte</span></div>
      <div class="stat"><b style="color:${s.errors ? "var(--bad)" : "inherit"}">${fmtNum(s.errors)}</b><span>Fehler</span></div>
    </div>
    ${repJobLine(r)}${repCurrent(r)}
    <div class="row spread" style="margin-top:10px"><span class="small muted">${last}</span>
      <div class="row"><button class="btn" onclick="repJob('abgleich')">Jetzt abgleichen</button>
      <button class="btn" onclick="repJob('balance')">Ausbalancieren</button></div></div>
    <details class="settings" id="rp-log" style="margin-top:12px" ${rp.logOpen ? "open" : ""}><summary>Aktivität</summary>
      <div style="max-height:300px;overflow:auto">${r.activity.length ? r.activity.map((a) => `<div class="row small" style="padding:4px 0;border-bottom:1px solid var(--line);flex-wrap:nowrap">
        <span>${ACT_ICON[a.kind] || "•"}</span><span class="muted" style="white-space:nowrap">${new Date(a.t * 1000).toLocaleTimeString("de-DE")}</span>
        <span style="word-break:break-all">${esc(a.text)}</span></div>`).join("") : '<div class="muted small">Noch keine Aktivität.</div>'}</div></details>
    <details class="settings" id="rp-cfg" style="margin-top:12px" ${rp.cfgOpen ? "open" : ""}><summary>Einstellungen Duplizierung</summary>
      <label class="row small"><input type="checkbox" id="rp-en" ${c.enabled ? "checked" : ""}> Duplizierung automatisch herstellen</label>
      <label class="row small"><input type="checkbox" id="rp-rt" ${c.realtime ? "checked" : ""}> In Echtzeit (neue und geänderte Dateien sofort duplizieren)</label>
      <label class="row small"><input type="checkbox" id="rp-ex" ${c.remove_excess ? "checked" : ""}> Überzählige Kopien entfernen (z. B. nach Senken des Solls)</label>
      <div class="settings-grid">
        <div class="field"><label>Ruhezeit vor dem Duplizieren (Sekunden)</label><input type="number" id="rp-delay" min="1" max="3600" value="${c.delay_seconds}"></div>
        <div class="field"><label>Mindestens frei lassen pro Platte (GB)</label><input type="number" id="rp-free" min="0" value="${c.min_free_gb}"></div>
        <div class="field"><label>Kopiergeschwindigkeit max. (MB/s, 0 = unbegrenzt)</label><input type="number" id="rp-speed" min="0" value="${c.max_mb_s}"></div>
        <div class="field"><label>Bei unterschiedlichen Kopien</label><select id="rp-conf">
          <option value="newest" ${c.conflict === "newest" ? "selected" : ""}>Neueste Kopie übernehmen</option>
          <option value="manual" ${c.conflict === "manual" ? "selected" : ""}>Nur melden, ich entscheide</option></select></div>
        <div class="field"><label>Täglicher Abgleich um</label><input type="time" id="rp-time" value="${esc(c.reconcile_time)}"></div>
      </div>
      <h3>Ausbalancieren</h3>
      <label class="row small"><input type="checkbox" id="rp-bal" ${c.balance_enabled ? "checked" : ""}> Täglich automatisch ausbalancieren</label>
      <div class="settings-grid">
        <div class="field"><label>Um</label><input type="time" id="rp-btime" value="${esc(c.balance_time)}"></div>
        <div class="field"><label>Ab Unterschied der Füllstände (%)</label><input type="number" id="rp-bthr" min="1" max="50" value="${c.balance_threshold_pct}"></div>
        <div class="field"><label>Höchstens verschieben pro Lauf (GB)</label><input type="number" id="rp-bmax" min="1" value="${c.balance_max_gb}"></div>
      </div>
      <div class="row" style="justify-content:flex-end"><button class="btn primary" onclick="saveRepCfg()">Speichern</button></div>
    </details>
  </div>`;
};

document.addEventListener("toggle", (e) => {
  if (e.target.id === "rp-cfg") rp.cfgOpen = e.target.open;
  if (e.target.id === "rp-log") rp.logOpen = e.target.open;
}, true);

window.saveRepCfg = () => act(() => api("rep/config", {
  enabled: $("#rp-en").checked, realtime: $("#rp-rt").checked, remove_excess: $("#rp-ex").checked,
  delay_seconds: +$("#rp-delay").value, min_free_gb: +$("#rp-free").value, max_mb_s: +$("#rp-speed").value,
  conflict: $("#rp-conf").value, reconcile_time: $("#rp-time").value, balance_enabled: $("#rp-bal").checked,
  balance_time: $("#rp-btime").value, balance_threshold_pct: +$("#rp-bthr").value, balance_max_gb: +$("#rp-bmax").value,
}), "Einstellungen gespeichert");

window.repJob = (kind, pool) => act(() => api("rep/start", { kind, pool }),
  kind === "abgleich" ? "Abgleich gestartet" : "Ausbalancieren gestartet");

window.removeMember = (pool, uuid, name) => {
  $("#dlg-body").innerHTML = `<h2>${esc(name)} aus dem Pool nehmen?</h2>
    <p>Alle Dateien, die danach fehlen oder unter ihr Duplizierungs-Soll fallen würden, werden vorher auf die
    anderen Platten kopiert. Erst wenn das vollständig geklappt hat, wird die Platte aus dem Pool genommen.</p>
    <p class="muted small">Auf der Platte selbst wird nichts gelöscht – die Dateien bleiben im PoolPart-Ordner erhalten
    und sind an jedem Rechner lesbar.</p>
    <div class="row" style="justify-content:flex-end"><button type="button" class="btn" onclick="$('#dlg').close()">Abbrechen</button>
      <button type="button" class="btn primary" id="rm-ok">Platte räumen und entfernen</button></div>`;
  $("#rm-ok").onclick = () => { $("#dlg").close(); act(() => api("rep/start", { kind: "entfernen", pool, uuid }), "Platte wird geräumt …"); };
  $("#dlg").showModal();
};

window.resolveDialog = async (pool, path) => {
  let copies;
  try { copies = await api(`rep/copies?pool=${encodeURIComponent(pool)}&path=${encodeURIComponent(path)}`); }
  catch (e) { toast(e.message); return; }
  const newest = Math.max(...copies.map((c) => c.mtime));
  $("#dlg-body").innerHTML = `<h2>Unterschiedliche Kopien</h2>
    <p class="mono small">${esc(path)}</p>
    <p class="muted small">Die Kopien auf den Platten sind nicht gleich. Wähle, welche Version behalten wird – sie wird auf alle anderen Platten übertragen.</p>
    <div class="picker">${copies.map((c) => `<div class="picker-row" style="grid-template-columns:1fr auto auto">
      <span class="picker-name">💽 ${esc(c.disk)} ${c.mtime === newest ? '<span class="badge ok">neueste</span>' : ""}</span>
      <span class="muted small">${fmtBytes(c.size)} · ${fmtTime(c.mtime)}</span>
      <button type="button" class="btn" onclick='resolvePick(${js(pool)}, ${js(path)}, ${js(c.uuid)})'>Diese behalten</button></div>`).join("")}</div>
    <div class="row" style="justify-content:flex-end;margin-top:12px"><button type="button" class="btn" onclick="$('#dlg').close()">Abbrechen</button></div>`;
  $("#dlg").showModal();
};
window.resolvePick = (pool, path, uuid) => { $("#dlg").close(); act(() => api("rep/resolve", { pool, path, uuid }), "Kopien angeglichen"); };
