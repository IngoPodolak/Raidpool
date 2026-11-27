"use strict";
// RAIDPool – SSD-Cache pro Pool

window.cacheLine = (p) => {
  const c = p.cache || {}; const cfg = c.config || {};
  if (!cfg.enabled) {
    return `<div class="row spread small" style="margin-top:10px;padding:10px;border-radius:10px;background:var(--bg)">
      <span class="muted">⚡ SSD-Cache aus</span><button class="btn" style="padding:2px 10px;font-size:12px" onclick='cacheDialog(${js(p.id)})'>Einrichten …</button></div>`;
  }
  const u = c.usage || {}; const m = c.mover || {};
  const max = cfg.max_gb * 1e9;
  const pct = max ? Math.min(100, Math.round((100 * (u.bytes || 0)) / max)) : 0;
  const target = cfg.target === "system" ? "System-SSD" : "eigene SSD";
  return `<div style="margin-top:10px;padding:10px;border-radius:10px;background:var(--bg)">
    <div class="row spread small"><span>⚡ <b>SSD-Cache</b> · ${target} ${c.active ? '<span class="badge ok">aktiv</span>' : '<span class="badge warn">nicht aktiv</span>'}</span>
      <span class="row"><button class="btn" style="padding:2px 10px;font-size:12px" ${u.files ? "" : "disabled"} onclick='act(()=>api("pools/cache_flush",{pool:${js(p.id)}}),"Dateien werden auf die Platten verschoben")'>Jetzt verschieben</button>
      <button class="btn" style="padding:2px 10px;font-size:12px" onclick='cacheDialog(${js(p.id)})'>Einstellungen</button></span></div>
    <div class="chartrow" style="margin:6px 0 0">${donut([{ v: u.bytes || 0, color: pct > 80 ? "var(--warn)" : "#f5b301", label: "Im Cache" }, { v: Math.max(0, max - (u.bytes || 0)), color: "transparent", label: "Frei" }], 46, pct + "%")}
      <div class="small">${fmtNum(u.files)} Dateien · ${fmtBytes(u.bytes)} von max. ${fmtBytes(max)}<br>
        <span class="muted">${m.running ? `Mover läuft: ${fmtNum(m.done)}/${fmtNum(m.total)}` : m.last ? "Zuletzt verschoben: " + fmtTime(m.last) : "Noch nichts verschoben"}
        · Dateien bleiben bis zum Verschieben nur 1× gespeichert</span></div></div>
    ${!c.active && p.read_only ? '<div class="small" style="color:var(--warn)">Schreibschutz aktiv – Cache ist erst ohne read_only aktiv.</div>' : ""}</div>`;
};

window.cacheDialog = async (pid) => {
  const p = state.status.pools.find((x) => x.id === pid);
  const cfg = Object.assign({ enabled: false, target: "system", max_gb: 50, min_free_gb: 20, move_after_min: 30, move_at_pct: 80, move_time: "03:30", hold_downloads: true, hold_hours: 24 }, (p.cache || {}).config || {});
  let targets = [];
  try { targets = await api("cache/targets"); } catch (e) { toast(e.message); }
  $("#dlg-body").innerHTML = `<h2>SSD-Cache für ${esc(p.name)}</h2>
    <p class="muted small" style="margin-top:0">Neue Dateien werden zuerst auf die SSD geschrieben (schnell) und später automatisch auf die Platten
      verschoben – dort dupliziert. Der Cache zählt nicht zum Pool-Speicher. Bis zum Verschieben liegt eine Datei nur einmal (auf der SSD).</p>
    <label class="row small"><input type="checkbox" id="cc-en" ${cfg.enabled ? "checked" : ""}> SSD-Cache für diesen Pool einschalten</label>
    <h3>SSD</h3>
    <div class="picker">${targets.map((t) => `<label class="picker-row" style="grid-template-columns:auto 1fr auto">
      <input type="radio" name="cc-t" value="${esc(t.id)}" ${t.id === cfg.target ? "checked" : ""} ${t.writable === false ? "disabled" : ""}>
      <span><b>${t.id === "system" ? "🏠" : t.ssd ? "⚡" : "💽"} ${esc(t.name)}</b>${t.id === "system" ? '<br><span class="muted small">Ordner im HA-Datenbereich, nicht in HA-Backups enthalten</span>' : t.ssd ? "" : '<br><span class="muted small">keine SSD – bringt kaum Geschwindigkeit</span>'}
        ${t.writable === false ? '<br><span class="small" style="color:var(--warn)">nur lesend eingebunden</span>' : ""}</span>
      <span class="small">${fmtBytes(t.free)} frei</span></label>`).join("")}</div>
    <p class="muted small">Eine SSD kann von mehreren Pools gleichzeitig genutzt werden – jeder Pool bekommt einen eigenen Ordner.</p>
    <div class="settings-grid">
      <div class="field"><label>Cache höchstens (GB)</label><input type="number" id="cc-max" min="1" value="${cfg.max_gb}"></div>
      <div class="field"><label>Auf der SSD immer frei lassen (GB)</label><input type="number" id="cc-free" min="1" value="${cfg.min_free_gb}"></div>
      <div class="field"><label>Verschieben nach … Minuten ohne Änderung (0 = nur nachts/bei Füllstand)</label><input type="number" id="cc-age" min="0" value="${cfg.move_after_min}"></div>
      <div class="field"><label>Sofort verschieben ab Füllstand (%)</label><input type="number" id="cc-pct" min="10" max="100" value="${cfg.move_at_pct}"></div>
      <div class="field"><label>Täglich alles verschieben um</label><input type="time" id="cc-time" value="${esc(cfg.move_time)}"></div>
      <div class="field"><label>Archivteile höchstens … Stunden halten</label><input type="number" id="cc-hold" min="1" max="720" value="${cfg.hold_hours}"></div>
    </div>
    <label class="row small"><input type="checkbox" id="cc-dl" ${cfg.hold_downloads ? "checked" : ""}> Downloads auf der SSD fertigstellen
      <span class="muted">– Archiv-/Downloadteile (.rar, .part, .7z, .zip …) bleiben auf der SSD (werden nach dem Entpacken meist gelöscht);
      ein Ordner wird erst verschoben, wenn darin so viele Minuten nichts mehr passiert ist (Download/Entpacken fertig). Wird die SSD voll, wird trotzdem verschoben.</span></label>
    <p class="muted small">Bei der System-SSD unbedingt genug für Home Assistant frei lassen. Beim Ein-/Ausschalten oder Wechsel der SSD wird der Pool kurz neu eingehängt
      (SMB-Verbindungen zu diesem Pool werden kurz unterbrochen); beim Ausschalten wird der Cache vorher geleert.</p>
    <div class="row" style="justify-content:flex-end"><button type="button" class="btn" onclick="$('#dlg').close()">Abbrechen</button>
      <button type="button" class="btn primary" id="cc-ok">Speichern</button></div>`;
  $("#cc-ok").onclick = async () => {
    const t = document.querySelector('input[name="cc-t"]:checked');
    const body = { pool: pid, enabled: $("#cc-en").checked, target: t ? t.value : "system", max_gb: +$("#cc-max").value,
      min_free_gb: +$("#cc-free").value, move_after_min: +$("#cc-age").value, move_at_pct: +$("#cc-pct").value, move_time: $("#cc-time").value,
      hold_downloads: $("#cc-dl").checked, hold_hours: +$("#cc-hold").value };
    const btn = $("#cc-ok"); btn.disabled = true; btn.textContent = "Speichere …";
    try { await api("pools/cache", body); $("#dlg").close(); toast("SSD-Cache gespeichert"); }
    catch (e) { btn.disabled = false; btn.textContent = "Speichern"; toast("Fehler: " + e.message); }
    refresh();
  };
  $("#dlg").showModal();
};
