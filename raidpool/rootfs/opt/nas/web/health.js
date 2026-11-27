"use strict";
// HA RAIDPool – Gesundheit: Ausfälle, Platte hinzufügen/ersetzen, Dateisystem-Prüfung, SMART-Test, USV

const hp = { cfgOpen: false, ups: null };
const FSCK_FS = ["ntfs", "ext4", "xfs", "btrfs", "exfat"];

window.healthBanner = () => {
  const h = state.health;
  if (!h) return "";
  let html = "";
  for (const f of h.failed) {
    const pool = (state.status.pools.find((p) => p.id === f.pool) || {}).name || f.pool;
    html += `<div class="banner warn row spread"><div>⚠️ <b>Platte ${esc(f.label)} ausgefallen</b> (Pool ${esc(pool)}, seit ${fmtTime(f.since)}).
      Der Pool läuft weiter, fehlende Kopien werden auf die übrigen Platten kopiert.</div>
      <button class="btn primary" onclick='memberDialog(${js(f.pool)}, ${js(f.uuid)}, ${js(f.label)})'>Platte ersetzen …</button></div>`;
  }
  if (h.ups && h.ups.on_battery) html += `<div class="banner warn">🔋 <b>USV im Batteriebetrieb</b>${h.ups.battery != null ? ` (${h.ups.battery} %)` : ""} – Duplizierung und Duplikatsuche sind pausiert.</div>`;
  return html;
};

// ---------------------------------------------------------------- Platte hinzufügen / ersetzen
window.memberDialog = async (pool, oldUuid, oldName) => {
  state.disks = await api("disks");
  const used = new Set(state.status.pools.flatMap((p) => p.members.map((m) => m.uuid)));
  const cands = [];
  for (const d of state.disks) {
    if (d.system) continue;
    for (const p of d.partitions) if (!used.has(p.uuid) && (p.mountpoint || ["ntfs", "ext4", "xfs", "btrfs", "exfat"].includes(p.fstype))) cands.push({ d, p });
  }
  const ro = state.status.options.read_only;
  $("#dlg-body").innerHTML = `<h2>${oldUuid ? "Platte ersetzen" : "Platte zum Pool hinzufügen"}</h2>
    ${oldUuid ? `<p>Ersetzt <b>${esc(oldName)}</b>. Danach werden alle Dateien, die unter ihr Duplizierungs-Soll gefallen sind,
      automatisch auf die neue Platte kopiert. Auf der alten Platte wird nichts gelöscht.</p>` : `<p class="muted small">Neue Dateien landen danach auch auf dieser Platte, und fehlende Kopien werden ergänzt.</p>`}
    <div class="picker">${cands.length ? cands.map(({ d, p }, i) => `<label class="picker-row" style="grid-template-columns:auto 1fr auto">
      <input type="radio" name="md" value="${i}" ${i === 0 ? "checked" : ""}>
      <span><b>${d.network ? "🌐" : "💽"} ${esc(d.model || d.name)}</b> <span class="muted small">${esc(p.label || p.name)} · ${esc(p.fstype)} · ${fmtBytes(p.usage ? p.usage.total : p.size)}</span>
        ${p.poolparts.length ? '<br><span class="badge dup">enthält DrivePool-Ordner</span>' : ""}</span>
      <span class="muted small">${p.usage ? fmtBytes(p.usage.free) + " frei" : "nicht eingehängt"}</span></label>`).join("")
      : '<div class="muted small" style="padding:12px">Keine freie Platte gefunden. Neue Platte anschließen (unter Proxmox durchreichen) und in NTFS, ext4 oder XFS formatieren.</div>'}</div>
    <div class="field"><label>Einbinden als</label><select id="md-mode">
      <option value="pp" ${ro ? "disabled" : "selected"}>PoolPart-Ordner (vorhandene Dateien bleiben getrennt)${ro ? " – nur mit Schreibzugriff" : ""}</option>
      <option value="direct" ${ro ? "selected" : ""}>Vorhandenen Inhalt direkt einbinden</option></select></div>
    ${ro ? '<p class="muted small">Schreibschutz aktiv – neue Kopien werden erst nach Ausschalten von <code>read_only</code> erstellt.</p>' : ""}
    <div class="row" style="justify-content:flex-end;margin-top:12px"><button type="button" class="btn" onclick="$('#dlg').close()">Abbrechen</button>
      <button type="button" class="btn primary" id="md-ok" ${cands.length ? "" : "disabled"}>${oldUuid ? "Ersetzen" : "Hinzufügen"}</button></div>`;
  $("#md-ok").onclick = async () => {
    const sel = document.querySelector('input[name="md"]:checked');
    if (!sel) return;
    const c = cands[+sel.value];
    $("#dlg").close();
    try {
      const r = await api(oldUuid ? "pools/replace_member" : "pools/add_member", { pool, old: oldUuid, uuid: c.p.uuid, mode: $("#md-mode").value });
      toast((oldUuid ? "Platte ersetzt" : "Platte hinzugefügt") + (r.reconcile ? " – fehlende Kopien werden erstellt" : ""));
    } catch (e) { toast("Fehler: " + e.message); }
    refresh();
  };
  $("#dlg").showModal();
};

// ---------------------------------------------------------------- Dateisystem-Prüfung
window.fsckLine = (d, p) => {
  if (d.network || !FSCK_FS.includes(p.fstype)) return "";
  const h = state.health || {};
  const running = h.check && h.check.running;
  const tool = p.fstype === "ntfs" ? (h.ntfsck ? "ntfsck" : "ntfsfix") : "";
  return `<div class="row" style="margin-top:6px">
    ${p.dirty ? `<span class="badge warn" title="${esc(p.dirty)}">unsauber – Prüfung empfohlen</span>` : ""}
    ${p.forced_ro ? '<span class="badge">nur lesend</span>' : ""}${p.checking ? '<span class="badge info">wird geprüft</span>' : ""}
    <button class="btn" style="padding:2px 8px;font-size:12px" ${running ? "disabled" : ""} onclick='fsck(${js(p.uuid)}, false, ${js(p.label || p.name)}, ${js(tool)})'>Prüfen</button>
    ${p.fstype !== "btrfs" ? `<button class="btn" style="padding:2px 8px;font-size:12px" ${running ? "disabled" : ""} onclick='fsck(${js(p.uuid)}, true, ${js(p.label || p.name)}, ${js(tool)})'>Prüfen und reparieren</button>` : ""}</div>`;
};
window.fsck = (uuid, repair, name, tool) => {
  const ntfsfix = tool === "ntfsfix";
  $("#dlg-body").innerHTML = `<h2>${repair ? "Prüfen und reparieren" : "Dateisystem prüfen"}: ${esc(name)}</h2>
    <p>Die Platte wird dafür kurz aus dem Pool gelöst und ausgehängt – der Pool läuft mit den anderen Platten weiter.
    Dateien, die nur auf dieser Platte liegen, sind während der Prüfung nicht erreichbar. Bei großen Platten kann das länger dauern.</p>
    ${ntfsfix ? `<p class="muted small">Hinweis: ntfsck ist nicht verfügbar, es wird ntfsfix verwendet. Das behebt nur grundlegende Fehler und setzt das „unsauber“-Kennzeichen zurück – keine vollständige Prüfung wie chkdsk.</p>` : ""}
    ${repair ? '<p class="muted small">Gefundene Fehler werden automatisch behoben.</p>' : '<p class="muted small">Nur prüfen – es wird nichts verändert.</p>'}
    <div class="row" style="justify-content:flex-end"><button type="button" class="btn" onclick="$('#dlg').close()">Abbrechen</button>
      <button type="button" class="btn primary" id="fs-ok">Starten</button></div>`;
  $("#fs-ok").onclick = () => { $("#dlg").close(); act(() => api("health/check", { uuid, repair }), "Prüfung gestartet"); };
  $("#dlg").showModal();
};

// ---------------------------------------------------------------- SMART-Oberflächentest
window.smartTestLine = (d) => {
  if (d.network || !d.smart || !d.smart.available) return "";
  const t = (state.health && state.health.smart_tests || {})[d.path];
  return `<div class="row spread" style="margin-top:8px"><span class="small muted">Oberflächentest (SMART, wie chkdsk /r)</span>
    ${t && t.running ? `<span class="small">läuft · noch ${t.remaining ?? "?"} % <a href="#" onclick='smartPoll(${js(d.path)});return false'>aktualisieren</a></span>`
      : `<span class="row small">${t && t.result ? `<span class="muted">Letztes Ergebnis: ${esc(t.result)}</span>` : ""}
        <button class="btn" style="padding:2px 8px;font-size:12px" onclick='smartTest(${js(d.path)})'>Starten</button></span>`}</div>`;
};
window.smartTest = (device) => {
  if (!confirm("Ausführlichen SMART-Selbsttest starten?\n\nDie Platte prüft dabei selbst ihre gesamte Oberfläche. Das dauert je nach Größe mehrere Stunden, die Platte bleibt nutzbar.")) return;
  act(() => api("health/smart_test", { device }), "Oberflächentest gestartet");
};
window.smartPoll = (device) => act(() => api("health/smart_test?device=" + encodeURIComponent(device)));

// ---------------------------------------------------------------- Platten-Reiter: oben
window.healthTop = () => {
  const h = state.health;
  if (!h) return "";
  const c = h.config;
  const chk = h.check;
  let html = "";
  if (chk) {
    html += `<div class="card"><div class="row spread"><div class="phase">${chk.running ? '<div class="spinner"></div>' : chk.result === "ok" || chk.result === "repariert" ? "✅" : "⚠️"}
        <b>${chk.repair ? "Prüfen und reparieren" : "Prüfung"}: ${esc(chk.label)}</b> <span class="muted small">${esc(chk.tool)} · ${esc(chk.phase)}</span></div>
        <span>${chk.result ? `<span class="badge ${chk.result === "ok" || chk.result === "repariert" ? "ok" : "warn"}">${esc(chk.result)}</span>` : ""}</span></div>
      <pre class="mono" id="fsck-log" style="max-height:220px;overflow:auto;background:var(--bg);padding:10px;border-radius:8px;white-space:pre-wrap;margin:10px 0 0">${esc(h.check_log.join("\n"))}</pre></div>`;
  }
  const ups = h.ups || {};
  html += `<details class="card settings" id="hp-cfg" ${hp.cfgOpen ? "open" : ""}><summary>Sicherheit & Strom
      ${ups.state ? `<span class="badge ${ups.on_battery ? "warn" : "ok"}" style="margin-left:6px">USV: ${esc(ups.state)}${ups.battery != null ? " · " + ups.battery + " %" : ""}</span>` : ""}</summary>
    <label class="row small"><input type="checkbox" id="hp-notify" ${c.notify ? "checked" : ""}> Meldungen in Home Assistant anzeigen (Ausfall, Prüfung, Stromausfall)</label>
    <div class="field"><label>Unsaubere NTFS-Platten (z. B. nach Stromausfall)</label><select id="hp-dirty">
      <option value="readonly" ${c.dirty_policy === "readonly" ? "selected" : ""}>Nur lesend einbinden und melden (empfohlen)</option>
      <option value="repair" ${c.dirty_policy === "repair" ? "selected" : ""} ${h.ntfsck ? "" : "disabled"}>Automatisch prüfen und reparieren${h.ntfsck ? "" : " (ntfsck nicht verfügbar)"}</option>
      <option value="mount" ${c.dirty_policy === "mount" ? "selected" : ""}>Trotzdem normal einbinden</option></select></div>
    <h3>USV (über die NUT-Integration in Home Assistant)</h3>
    <div class="settings-grid">
      <div class="field"><label>Status-Sensor der USV</label><select id="hp-ups"><option value="">– keine USV –</option>${upsOptions(c.ups_entity, "status")}</select></div>
      <div class="field"><label>Akku-Sensor (optional)</label><select id="hp-batt"><option value="">–</option>${upsOptions(c.ups_battery_entity, "battery")}</select></div>
      <div class="field"><label>Akku niedrig ab (%)</label><input type="number" id="hp-low" min="5" max="90" value="${c.ups_low_battery_pct}"></div>
    </div>
    <div class="row small"><a href="#" onclick="loadUps();return false">USV-Sensoren suchen</a></div>
    <label class="row small"><input type="checkbox" id="hp-unmount" ${c.ups_unmount_on_low ? "checked" : ""}> Bei niedrigem Akku alle Pools und Platten sauber aushängen</label>
    <p class="muted small">Im Batteriebetrieb werden Duplizierung, Duplikatsuche und Ausbalancieren sofort pausiert und alle Daten auf die Platten geschrieben.</p>
    ${h.events.length ? `<h3>Letzte Ereignisse</h3>${h.events.slice(0, 8).map((e) => `<div class="small" style="padding:3px 0">${new Date(e.t * 1000).toLocaleString("de-DE")} – <b>${esc(e.title)}</b>: ${esc(e.message.replace(/\*\*/g, ""))}</div>`).join("")}` : ""}
    <div class="row" style="justify-content:flex-end"><button class="btn primary" onclick="saveHealth()">Speichern</button></div>
  </details>`;
  setTimeout(() => {
    const l = $("#fsck-log"); if (l) l.scrollTop = l.scrollHeight;
    const det = $("#hp-cfg"); if (det) det.ontoggle = () => { hp.cfgOpen = det.open; if (det.open && !hp.ups) loadUps(); };
  });
  return html;
};
function upsOptions(cur, kind) {
  const list = (hp.ups && hp.ups[kind]) || [];
  const opts = list.map((s) => `<option value="${esc(s.entity_id)}" ${s.entity_id === cur ? "selected" : ""}>${esc(s.name)} (${esc(s.state)})</option>`);
  if (cur && !list.some((s) => s.entity_id === cur)) opts.unshift(`<option value="${esc(cur)}" selected>${esc(cur)}</option>`);
  return opts.join("");
}
window.loadUps = async () => {
  try { hp.ups = await api("health/ups_candidates"); } catch (e) { toast(e.message); hp.ups = { status: [], battery: [] }; }
  if (state.tab === "disks") renderDisks();
};
window.saveHealth = () => act(() => api("health/config", {
  notify: $("#hp-notify").checked, dirty_policy: $("#hp-dirty").value, ups_entity: $("#hp-ups").value,
  ups_battery_entity: $("#hp-batt").value, ups_low_battery_pct: +$("#hp-low").value, ups_unmount_on_low: $("#hp-unmount").checked,
}), "Gespeichert");
