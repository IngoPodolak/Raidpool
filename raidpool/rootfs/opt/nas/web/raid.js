"use strict";
// RAIDPool – Reiter „RAID“ (Linux-Software-RAID mit mdadm)

const rd = { st: null };
const MEMBER_STATE = { in_sync: ["ok", "aktiv"], spare: ["info", "Reserve"], faulty: ["bad", "defekt"], write_mostly: ["", "write-mostly"] };
window.raidBusy = () => !!(rd.st && ((rd.st.job && rd.st.job.running) || rd.st.arrays.some((a) => a.sync_action && a.sync_action !== "idle")));

function capacity(level, sizes) {
  if (!sizes.length) return 0;
  const n = sizes.length, s = Math.min(...sizes);
  return { raid0: s * n, linear: sizes.reduce((a, b) => a + b, 0), raid1: s, raid5: s * (n - 1), raid6: s * (n - 2), raid10: Math.floor((s * n) / 2) }[level] || 0;
}

window.renderRaid = async function () {
  const el = $("#tab-raid");
  try { rd.st = await api("raid/status"); } catch (e) { el.innerHTML = `<div class="card">Fehler: ${esc(e.message)}</div>`; return; }
  const st = rd.st; const sup = st.support || {};
  const job = st.job;
  el.innerHTML = `
    <div class="row spread"><h2>RAID-Verbünde</h2>
      <button class="btn primary" ${sup.available ? "" : "disabled"} onclick="raidCreate()">+ RAID anlegen</button></div>
    ${!sup.available ? `<div class="banner warn" style="margin:0 0 16px">${esc(sup.reason || "Software-RAID ist auf diesem System nicht verfügbar.")}</div>` : ""}
    ${st.read_only ? '<div class="banner warn" style="margin:0 0 16px">Schreibschutz aktiv – zum Anlegen oder Reparieren eines RAID in den App-Optionen <code>read_only</code> ausschalten.</div>' : ""}
    ${job ? `<div class="card"><div class="row spread"><div class="phase">${job.running ? '<div class="spinner"></div>' : job.result === "ok" ? "✅" : "⚠️"}
        <b>RAID ${esc(job.name)} ${job.kind}</b> <span class="muted small">${esc(job.phase || "")}</span></div>
        ${job.result ? `<span class="badge ${job.result === "ok" ? "ok" : "bad"}">${job.result === "ok" ? "fertig" : "Fehler"}</span>` : ""}</div>
      <pre class="mono" style="max-height:160px;overflow:auto;background:var(--bg);padding:10px;border-radius:8px;white-space:pre-wrap;margin:10px 0 0">${esc((job.log || []).join("\n"))}</pre></div>` : ""}
    ${st.arrays.length ? `<div class="grid">${st.arrays.map(raidCard).join("")}</div>`
      : `<div class="card empty">Noch kein RAID-Verbund.<br><span class="small">Für einen Verbund werden leere Platten benötigt – beim Anlegen werden alle Daten auf den gewählten Platten gelöscht.</span></div>`}
    <div class="card muted small">💡 Ein RAID-Verbund erscheint danach unter „Platten“ und kann wie eine Platte in einen Pool aufgenommen
      (Übersicht → „+ Pool“ oder „+ Platte hinzufügen“) und damit freigegeben werden. Einzelne Platten eines RAID 0/5/6/10 sind an anderen Rechnern nicht lesbar.</div>`;
};

function raidCard(a) {
  const syncing = a.sync_action && a.sync_action !== "idle";
  const act = { resync: "Erstsynchronisation", recover: "Wiederaufbau", check: "Prüfung", repair: "Reparatur", reshape: "Umbau" }[a.sync_action] || a.sync_action;
  const badge = a.degraded ? `<span class="badge bad">beeinträchtigt (${a.degraded} fehlt)</span>` : syncing ? `<span class="badge info">${esc(act)}</span>` : '<span class="badge ok">in Ordnung</span>';
  return `<div class="card">
    <div class="row spread"><b style="font-size:15px">🧱 ${esc(a.name)}</b>${badge}</div>
    <div class="muted small">${esc(a.level.toUpperCase().replace("RAID", "RAID "))} · ${fmtBytes(a.size)} · ${a.raid_disks} Platten · <span class="mono">${esc(a.device)}</span></div>
    ${syncing && a.sync_progress != null ? `<div style="margin-top:8px"><div class="small">${esc(act)}: ${a.sync_progress} %</div><div class="progress big"><div style="width:${a.sync_progress}%"></div></div></div>` : ""}
    ${a.mismatch ? `<div class="small" style="color:var(--warn);margin-top:6px">⚠️ ${fmtNum(a.mismatch)} Abweichungen bei der letzten Prüfung</div>` : ""}
    <h3>Platten</h3>
    ${a.members.map((m) => { const s = MEMBER_STATE[(m.state || "").split(",")[0]] || ["", m.state];
      return `<div class="row spread small" style="padding:4px 0;border-top:1px solid var(--line)"><span class="mono">${esc(m.device)}</span><span class="badge ${s[0]}">${esc(s[1])}</span></div>`; }).join("")}
    <div class="row" style="margin-top:12px">
      <button class="btn" ${syncing ? "disabled" : ""} onclick='act(()=>api("raid/check",{name:${js(a.name)}}),"Prüfung gestartet")'>Prüfen</button>
      <button class="btn ${a.degraded ? "primary" : ""}" onclick='raidReplace(${js(a.name)})'>Platte ersetzen …</button>
      <button class="btn danger" onclick='raidDelete(${js(a.name)})'>Löschen …</button></div>
  </div>`;
}

window.raidCreate = () => {
  const st = rd.st;
  const usable = st.candidates.filter((c) => !c.blocked);
  const draw = () => {
    const level = $("#rc-level").value;
    const sel = [...document.querySelectorAll("[data-rc]:checked")].map((x) => +x.dataset.rc);
    const sizes = sel.map((i) => st.candidates[i].size);
    const cap = capacity(level, sizes);
    const need = st.min[level];
    $("#rc-info").innerHTML = sel.length < need ? `<span style="color:var(--warn)">Mindestens ${need} Platten auswählen</span>`
      : `Nutzbarer Speicher: <b>${fmtBytes(cap)}</b> von ${fmtBytes(sizes.reduce((a, b) => a + b, 0))}` +
        (level === "raid10" && sel.length % 2 ? ' – <span style="color:var(--warn)">gerade Anzahl nötig</span>' : "") +
        (new Set(sizes).size > 1 && level !== "linear" ? '<br><span class="muted">Unterschiedlich große Platten: es wird nur die Größe der kleinsten genutzt.</span>' : "");
  };
  $("#dlg-body").innerHTML = `<h2>RAID anlegen</h2>
    <div class="settings-grid">
      <div class="field"><label>Name</label><input type="text" id="rc-name" placeholder="raid1" maxlength="24"></div>
      <div class="field"><label>Typ</label><select id="rc-level">${Object.entries(st.levels).map(([k, l]) =>
        `<option value="${k}" ${k === "raid5" ? "selected" : ""}>${esc(l)}${st.support.levels.length && !st.support.levels.includes(k) && k !== "linear" ? " (Kern-Modul fehlt evtl.)" : ""}</option>`).join("")}</select></div>
      <div class="field"><label>Dateisystem</label><select id="rc-fs"><option value="ext4">ext4</option><option value="xfs">XFS</option></select></div>
    </div>
    <h3>Platten</h3>
    <div class="picker">${st.candidates.length ? st.candidates.map((c, i) => `<label class="picker-row" style="grid-template-columns:auto 1fr auto;${c.blocked ? "opacity:.55;cursor:default" : ""}">
      <input type="checkbox" data-rc="${i}" ${c.blocked ? "disabled" : ""}>
      <span><b>💽 ${esc(c.model || c.name)}</b> <span class="muted small mono">${esc(c.path)}</span><br>
        <span class="small muted">${c.partitions.length ? "enthält: " + c.partitions.map((p) => `${esc(p.label || "?")} (${esc(p.fstype || "leer")}, ${fmtBytes(p.size)})`).join(", ") : "leer"}</span>
        ${c.blocked ? `<br><span class="small" style="color:var(--warn)">nicht verwendbar: ${esc(c.blocked)}</span>` : ""}</span>
      <span class="small">${fmtBytes(c.size)}</span></label>`).join("") : '<div class="muted small" style="padding:12px">Keine Platten gefunden.</div>'}</div>
    <div class="small" id="rc-info" style="margin:8px 0"></div>
    <div class="banner warn" style="margin:10px 0">⚠️ <b>Alle Daten auf den gewählten Platten werden unwiderruflich gelöscht.</b></div>
    <div class="field"><label>Zur Bestätigung den Namen des Verbunds eintippen</label><input type="text" id="rc-confirm" autocomplete="off"></div>
    <div class="row" style="justify-content:flex-end;margin-top:10px"><button type="button" class="btn" onclick="$('#dlg').close()">Abbrechen</button>
      <button type="button" class="btn primary" style="background:var(--bad);border-color:var(--bad)" id="rc-ok" ${usable.length ? "" : "disabled"}>Platten löschen und RAID anlegen</button></div>`;
  document.querySelectorAll("[data-rc]").forEach((x) => (x.onchange = draw));
  $("#rc-level").onchange = draw;
  draw();
  $("#rc-ok").onclick = async () => {
    const devices = [...document.querySelectorAll("[data-rc]:checked")].map((x) => st.candidates[+x.dataset.rc].path);
    try {
      await api("raid/create", { name: $("#rc-name").value.trim(), level: $("#rc-level").value, fs: $("#rc-fs").value, devices, confirm: $("#rc-confirm").value.trim() });
      $("#dlg").close(); toast("RAID wird angelegt …");
    } catch (e) { toast("Fehler: " + e.message); }
    refresh();
  };
  $("#dlg").showModal();
};

window.raidReplace = (name) => {
  const a = rd.st.arrays.find((x) => x.name === name);
  const usable = rd.st.candidates.filter((c) => !c.blocked);
  const faulty = a.members.filter((m) => (m.state || "").includes("faulty"));
  $("#dlg-body").innerHTML = `<h2>Platte in ${esc(name)} ersetzen</h2>
    <div class="field"><label>Defekte / zu ersetzende Platte</label><select id="rr-old">
      <option value="">– keine (nur neue Platte hinzufügen, z. B. als Reserve) –</option>
      ${a.members.map((m) => `<option value="${esc(m.device)}" ${faulty.some((f) => f.device === m.device) ? "selected" : ""}>${esc(m.device)} (${esc(m.state)})</option>`).join("")}</select></div>
    <div class="field"><label>Neue Platte (wird gelöscht)</label><select id="rr-new">${usable.map((c) => `<option value="${esc(c.path)}">${esc(c.model || c.name)} · ${esc(c.path)} · ${fmtBytes(c.size)}</option>`).join("") || "<option value=''>keine freie Platte</option>"}</select></div>
    <p class="muted small">Danach wird der Verbund automatisch wiederaufgebaut – das kann je nach Größe Stunden dauern. Die Daten bleiben dabei erreichbar.</p>
    <div class="row" style="justify-content:flex-end"><button type="button" class="btn" onclick="$('#dlg').close()">Abbrechen</button>
      <button type="button" class="btn primary" id="rr-ok" ${usable.length ? "" : "disabled"}>Ersetzen</button></div>`;
  $("#rr-ok").onclick = () => { $("#dlg").close(); act(() => api("raid/replace", { name, failed: $("#rr-old").value, device: $("#rr-new").value }), "Platte hinzugefügt – Wiederaufbau läuft"); };
  $("#dlg").showModal();
};

window.raidDelete = (name) => {
  $("#dlg-body").innerHTML = `<h2>RAID ${esc(name)} löschen?</h2>
    <p><b style="color:var(--bad)">Alle Daten im Verbund gehen verloren.</b> Die Platten werden danach wieder als leere Platten angezeigt.</p>
    <div class="field"><label>Zur Bestätigung den Namen eintippen</label><input type="text" id="rx-confirm" autocomplete="off"></div>
    <div class="row" style="justify-content:flex-end"><button type="button" class="btn" onclick="$('#dlg').close()">Abbrechen</button>
      <button type="button" class="btn primary" style="background:var(--bad);border-color:var(--bad)" id="rx-ok">Endgültig löschen</button></div>`;
  $("#rx-ok").onclick = () => { const c = $("#rx-confirm").value.trim(); $("#dlg").close(); act(() => api("raid/delete", { name, confirm: c }), "Verbund gelöscht"); };
  $("#dlg").showModal();
};
