"use strict";
// RAIDPool – Reiter „Freigaben“ (SMB)

const sh = { st: null, draft: null, cfgOpen: false };
const ACC_LABEL = { none: "Kein Zugriff", ro: "Lesen", rw: "Lesen + Schreiben" };

function accSeg(name, cur, onclick) {
  return `<div class="seg acc">${["none", "ro", "rw"].map((a) =>
    `<button type="button" class="${cur === a ? "active acc-" + a : ""}" onclick='${onclick}(${js(name)}, "${a}")'>${ACC_LABEL[a]}</button>`).join("")}</div>`;
}

window.renderShares = async function () {
  const el = $("#tab-shares");
  try { sh.st = await api("smb/status"); } catch (e) { el.innerHTML = `<div class="card">Fehler: ${esc(e.message)}</div>`; return; }
  const st = sh.st; const c = st.config;
  const stateBadge = !c.enabled ? '<span class="badge">ausgeschaltet</span>'
    : st.error ? '<span class="badge bad">Fehler</span>' : st.running ? '<span class="badge ok">läuft</span>' : '<span class="badge bad">gestoppt</span>';
  el.innerHTML = `
  <div class="card hero" style="background:linear-gradient(135deg, color-mix(in srgb, var(--accent) 14%, var(--card)), var(--card) 70%)">
    <div><div class="big" style="font-size:26px">\\\\${esc(st.server_name)}</div>
      ${c.aliases.length ? `<div class="small" style="margin-top:4px">auch erreichbar als ${c.aliases.map((a) => `<b class="mono">\\\\${esc(a)}</b>`).join(", ")}</div>` : ""}
      ${(st.ips || []).length ? `<div class="small muted">zusätzliche IP: ${st.ips.map((i) => esc(i.ip) + (i.active ? " ✓" : " ⚠️")).join(", ")}</div>` : ""}
      <div class="muted small" style="margin-top:6px">SMB-Server ${stateBadge} · Arbeitsgruppe ${esc(c.workgroup)}</div></div>
    <div>
      <div class="stats" style="margin-top:0">
        <div class="stat"><b>${st.shares.filter((s) => s.enabled).length}</b><span>Freigaben aktiv</span></div>
        <div class="stat"><b>${st.users.length}</b><span>Benutzer</span></div>
        <div class="stat"><b>${st.sessions.length}</b><span>verbunden</span></div>
      </div>
      <div class="row" style="justify-content:flex-end">
        ${st.running && c.enabled ? `<button class="btn" onclick="act(()=>api('smb/restart',{}),'Samba neu gestartet')">Neu starten</button>
          <button class="btn" onclick="act(()=>api('smb/stop',{}),'Samba gestoppt')">Stoppen</button>`
          : c.enabled ? `<button class="btn primary" onclick="act(()=>api('smb/start',{}),'Samba gestartet')">Starten</button>` : ""}
      </div>
    </div>
  </div>
  ${st.error ? `<div class="banner warn" style="margin:0 0 16px">${esc(st.error)}</div>` : ""}
  ${st.read_only ? `<div class="banner warn" style="margin:0 0 16px"><b>Schreibschutz aktiv:</b> alle Freigaben sind nur lesbar, bis <code>read_only</code> ausgeschaltet wird.</div>` : ""}

  <div class="row spread"><h2>Freigaben</h2><div class="row">
    <button class="btn" onclick="importDialog()">Vom alten Server übernehmen …</button>
    <button class="btn" onclick="newShare(true)">Ganzen Pool freigeben</button>
    <button class="btn primary" onclick="newShare(false)">+ Ordner freigeben</button></div></div>
  ${st.shares.length ? `<div class="grid">${st.shares.map(shareCard).join("")}</div>`
    : '<div class="card empty">Noch keine Freigabe. Gib den ganzen Pool oder einzelne Ordner daraus frei – beides gleichzeitig ist möglich.</div>'}

  ${directCard(st)}

  <div class="row spread" style="margin-top:8px"><h2>Benutzer</h2><div class="row">
    <button class="btn" onclick="importHaUsers()">Aus Home Assistant übernehmen</button>
    <button class="btn" onclick="userDialog()">+ Benutzer</button></div></div>
  <div class="card" style="padding:0">${st.users.length ? st.users.map(userRow).join("")
    : '<div class="empty">Noch keine Benutzer. Ohne Benutzer funktionieren nur Freigaben mit Gastzugriff.</div>'}</div>

  ${st.sessions.length ? `<h2>Aktive Verbindungen</h2><div class="card table-wrap"><table><tr><th>Benutzer</th><th>Gerät</th><th>Freigaben</th><th>Protokoll</th></tr>
    ${st.sessions.map((x) => `<tr><td>${esc(x.user)}</td><td>${esc(x.host)}</td><td>${esc(x.shares.join(", "))}</td><td>${esc(x.protocol || "")}${x.encrypted ? " 🔒" : ""}</td></tr>`).join("")}</table></div>` : ""}

  <details class="card settings" id="smb-cfg" ${sh.cfgOpen ? "open" : ""}><summary>Server-Einstellungen</summary>
    <div class="settings-grid">
      <div class="field"><label>Servername <span class="muted small">(leer = ${esc(st.server_name)})</span></label><input type="text" id="smb-name" maxlength="15" value="${esc(c.server_name)}"></div>
      <div class="field"><label>Arbeitsgruppe</label><input type="text" id="smb-wg" value="${esc(c.workgroup)}"></div>
      <div class="field"><label>Mindest-Protokoll</label><select id="smb-proto">
        ${[["SMB2_10", "SMB 2.1 (empfohlen)"], ["SMB3", "SMB 3 (sicherer, ältere Geräte gehen evtl. nicht)"], ["NT1", "SMB 1 (alt, unsicher – nur für sehr alte Geräte)"]]
          .map(([v, l]) => `<option value="${v}" ${c.min_protocol === v ? "selected" : ""}>${l}</option>`).join("")}</select></div>
    </div>
    <div class="field"><label>Weitere Servernamen <span class="muted small">(z. B. der Name des alten Windows-Servers – Geräte müssen nichts ändern)</span></label>
      <div class="excl">${c.aliases.length ? c.aliases.map((a) => `<span class="chip">🖥️ \\\\${esc(a)}<button type="button" onclick='removeAlias(${js(a)})'>✕</button></span>`).join("") : '<span class="muted small">Keine</span>'}</div>
      <div class="row"><input type="text" id="smb-alias" maxlength="15" placeholder="SERVER" style="text-transform:uppercase;max-width:200px">
        <button type="button" class="btn" onclick="addAlias()">Hinzufügen</button></div></div>
    <div class="field"><label>Zusätzliche IP-Adressen <span class="muted small">(z. B. die alte IP des Windows-Servers, falls Geräte \\\\192.168… nutzen)</span></label>
      <div class="excl">${(st.ips || []).length ? st.ips.map((i) => `<span class="chip">${i.active ? "✅" : "⚠️"} ${esc(i.ip)}${i.error ? ` <span class="muted small">${esc(i.error)}</span>` : ""}
        <button type="button" onclick='removeIp(${js(i.ip)})'>✕</button></span>`).join("") : '<span class="muted small">Keine</span>'}</div>
      <div class="row"><input type="text" id="smb-ip" placeholder="192.168.0.10" style="max-width:200px">
        <button type="button" class="btn" onclick="addIp()">Hinzufügen</button></div>
      <span class="muted small">Eigene Adresse(n): ${esc((st.own_ips || []).join(", "))}. Die alte Adresse im Router für DHCP sperren, damit sie nicht neu vergeben wird.</span></div>
    <label class="row small"><input type="checkbox" id="smb-en" ${c.enabled ? "checked" : ""}> SMB-Server eingeschaltet</label>
    <label class="row small"><input type="checkbox" id="smb-wsdd" ${c.wsdd ? "checked" : ""}> In der Windows-Netzwerkumgebung anzeigen (WS-Discovery)</label>
    <label class="row small"><input type="checkbox" id="smb-nb" ${c.netbios ? "checked" : ""}> NetBIOS – Namensauflösung im Netzwerk (bei weiteren Servernamen automatisch an)</label>
    <div class="row spread" style="margin-top:10px"><a href="#" class="small" onclick="showConf();return false">Erzeugte smb.conf ansehen</a>
      <button class="btn primary" onclick="saveSmbCfg()">Speichern</button></div>
  </details>`;
  const det = $("#smb-cfg"); det.ontoggle = () => { sh.cfgOpen = det.open; };
};

function shareCard(s) {
  const users = Object.entries(s.access || {}).filter(([, a]) => a !== "none");
  const where = s.path ? `${esc(s.pool_name || s.pool)} › ${esc(s.path)}` : `${esc(s.pool_name || s.pool)} <span class="muted">(ganzer Pool)</span>`;
  return `<div class="card" style="${s.enabled ? "" : "opacity:.6"}">
    <div class="row spread"><b style="font-size:15px">${s.path ? "📁" : "🗄️"} ${esc(s.name)}</b>
      <div class="row">${s.enabled ? "" : '<span class="badge">deaktiviert</span>'}${s.path_ok ? "" : '<span class="badge bad">Ordner fehlt</span>'}</div></div>
    <div class="row" style="margin:4px 0"><span class="mono small">${esc(s.unc)}</span>
      <button class="btn" style="padding:2px 8px" title="Kopieren" onclick='copyText(${js(s.unc)})'>⧉</button></div>
    <div class="small muted">📂 ${where}${s.comment ? " · " + esc(s.comment) : ""}</div>
    <div class="row" style="margin-top:8px">
      ${s.guest !== "none" ? `<span class="badge warn">Gast: ${s.guest === "rw" ? "lesen + schreiben" : "lesen"}</span>` : ""}
      ${s.browseable ? "" : '<span class="badge">versteckt</span>'}
      ${s.recycle && !s.timemachine ? '<span class="badge">Papierkorb</span>' : ""}
      ${s.mac ? '<span class="badge">macOS</span>' : ""}
      ${s.timemachine ? '<span class="badge info">Time Machine</span>' : ""}</div>
    <div class="small" style="margin-top:8px">${users.length ? users.map(([u, a]) => `<span class="chip" style="padding:2px 10px">👤 ${esc(u)} · ${a === "rw" ? "lesen + schreiben" : "lesen"}</span>`).join(" ")
      : s.guest === "none" ? '<span class="muted">Noch kein Benutzer berechtigt</span>' : ""}</div>
    <div class="row" style="margin-top:12px">
      <button class="btn" onclick='editShare(${js(s.id)})'>Bearbeiten</button>
      <button class="btn" onclick='toggleShare(${js(s.id)})'>${s.enabled ? "Deaktivieren" : "Aktivieren"}</button>
      <button class="btn danger" onclick='removeShare(${js(s.id)}, ${js(s.name)})'>Entfernen</button></div>
  </div>`;
}

function userRow(u) {
  const n = sh.st.shares.filter((s) => (s.access || {})[u.name] && s.access[u.name] !== "none").length;
  return `<div class="row spread" style="padding:12px 16px;border-bottom:1px solid var(--line)">
    <div class="row"><div class="ficon" style="width:34px;height:34px;font-size:16px">👤</div>
      <div><b>${esc(u.display || u.name)}</b> <span class="muted small mono">${esc(u.name)}</span><br>
      <span class="small muted">${u.ha ? '<span class="badge info">Home Assistant</span>' : '<span class="badge">nur SMB</span>'} · ${n} Freigaben${u.enabled === false ? ' · <span class="badge bad">gesperrt</span>' : ""}</span>
      ${(u.logins || []).length ? `<br><span class="small muted">meldet sich auch an als: ${u.logins.map((x) => `<span class="mono">${esc(x)}</span>`).join(", ")}</span>` : ""}</div></div>
    <div class="row">
      <button class="btn" onclick='loginsDialog(${js(u.name)})'>Anmeldenamen</button>
      <button class="btn" onclick='pwDialog(${js(u.name)})'>Passwort ändern</button>
      <button class="btn" onclick='act(()=>api("smb/user/enabled",{name:${js(u.name)},enabled:${u.enabled === false}}),"Gespeichert")'>${u.enabled === false ? "Entsperren" : "Sperren"}</button>
      <button class="btn danger" onclick='removeUser(${js(u.name)})'>Entfernen</button></div></div>`;
}

window.copyText = (t) => { navigator.clipboard && navigator.clipboard.writeText(t); toast("Kopiert: " + t); };
window.toggleShare = (id) => {
  const s = sh.st.shares.find((x) => x.id === id);
  act(() => api("smb/share/save", Object.assign({}, s, { enabled: !s.enabled })), s.enabled ? "Freigabe deaktiviert" : "Freigabe aktiviert");
};
window.removeShare = (id, name) => {
  if (!confirm(`Freigabe „${name}“ entfernen?\n\nEs werden keine Dateien gelöscht.`)) return;
  act(() => api("smb/share/remove", { id }), "Freigabe entfernt");
};
window.removeUser = (name) => {
  if (!confirm(`Benutzer „${name}“ entfernen? Er verliert den Zugriff auf alle Freigaben.`)) return;
  act(() => api("smb/user/remove", { name }), "Benutzer entfernt");
};
window.saveSmbCfg = () => act(async () => {
  await api("smb/config", { server_name: $("#smb-name").value.trim(), workgroup: $("#smb-wg").value.trim() || "WORKGROUP",
    min_protocol: $("#smb-proto").value, enabled: $("#smb-en").checked, wsdd: $("#smb-wsdd").checked, netbios: $("#smb-nb").checked });
}, "Server-Einstellungen gespeichert");
window.showConf = async () => {
  const r = await api("smb/conf");
  $("#dlg-body").innerHTML = `<h2>smb.conf</h2><pre class="mono" style="max-height:60vh;overflow:auto;background:var(--bg);padding:10px;border-radius:8px;white-space:pre-wrap">${esc(r.text)}</pre>
    <div class="row" style="justify-content:flex-end"><button class="btn" value="cancel">Schließen</button></div>`;
  $("#dlg").showModal();
};

// ---------------------------------------------------------------- Freigabe-Dialog
window.newShare = (wholePool) => {
  const pools = state.status.pools;
  if (!pools.length) { toast("Zuerst einen Pool anlegen"); return; }
  const access = {};
  for (const u of sh.st.users) access[u.name] = "rw";
  sh.draft = { name: wholePool ? pools[0].name : "", pool: pools[0].id, path: "", comment: "", enabled: true, browseable: true,
    guest: "none", recycle: true, mac: false, timemachine: false, tm_size_gb: 0, access };
  if (wholePool) { renderShareDialog(); $("#dlg").showModal(); }
  else pickShareFolder(true);
};
window.editShare = (id) => {
  sh.draft = JSON.parse(JSON.stringify(sh.st.shares.find((s) => s.id === id)));
  renderShareDialog(); $("#dlg").showModal();
};
function pickShareFolder(fresh) {
  const d = sh.draft;
  openFolderPicker((pool, path) => {
    d.pool = pool; d.path = path;
    if (fresh || !d.name) d.name = path ? path.split("/").pop() : (state.status.pools.find((p) => p.id === pool) || {}).name || pool;
    renderShareDialog();
  }, d.pool, d.path, { title: "Ordner zum Freigeben wählen", ok: "Diesen Ordner wählen", fixedPool: false, allowRoot: true,
    markExcluded: false, keepOpen: true, onCancel: () => (fresh ? $("#dlg").close() : renderShareDialog()) });
}
window.pickShareFolder = pickShareFolder;
window.shAcc = (name, a) => { sh.draft.access[name] = a; renderShareDialog(); };
window.shGuest = (_, a) => { sh.draft.guest = a; renderShareDialog(); };
window.shSet = (k, v) => {
  sh.draft[k] = v;
  if (k === "timemachine" && v) { sh.draft.recycle = false; sh.draft.mac = true; }
  if (k === "timemachine" || k === "pool") renderShareDialog();
};

function renderShareDialog() {
  const d = sh.draft; const pools = state.status.pools;
  const pname = (pools.find((p) => p.id === d.pool) || {}).name || d.pool;
  $("#dlg-body").innerHTML = `<h2>${d.id ? "Freigabe bearbeiten" : "Neue Freigabe"}</h2>
    <div class="settings-grid">
      <div class="field"><label>Name der Freigabe</label><input type="text" id="sd-name" value="${esc(d.name)}" oninput="sh.draft.name=this.value" placeholder="z. B. Filme"></div>
      <div class="field"><label>Beschreibung <span class="muted small">(optional)</span></label><input type="text" id="sd-comment" value="${esc(d.comment)}" oninput="sh.draft.comment=this.value"></div>
    </div>
    <div class="field"><label>Freigegebener Ordner</label>
      <div class="row spread" style="border:1px solid var(--line);border-radius:8px;padding:8px 10px">
        <span>${d.path ? "📁 " + esc(pname) + " › <b>" + esc(d.path) + "</b>" : "🗄️ <b>" + esc(pname) + "</b> (ganzer Pool)"}</span>
        <button type="button" class="btn" onclick="pickShareFolder(false)">Ändern …</button></div>
      <span class="muted small mono">\\\\${esc(sh.st.server_name)}\\${esc(d.name || "…")}</span></div>
    <h3>Zugriff</h3>
    ${sh.st.users.length ? `<div class="acc-table">${sh.st.users.map((u) => `<div class="row spread acc-row"><span>👤 ${esc(u.display || u.name)}</span>${accSeg(u.name, d.access[u.name] || "none", "shAcc")}</div>`).join("")}</div>`
      : '<div class="muted small">Noch keine Benutzer angelegt – unten bei „Benutzer“ hinzufügen oder Gastzugriff erlauben.</div>'}
    <div class="row spread acc-row" style="margin-top:6px"><span>👥 Gast (ohne Anmeldung)</span>${accSeg("guest", d.guest, "shGuest")}</div>
    ${d.guest !== "none" ? '<div class="muted small">Jeder im Netzwerk kann ohne Passwort zugreifen.</div>' : ""}
    <h3>Optionen</h3>
    <label class="row small"><input type="checkbox" ${d.browseable ? "checked" : ""} onchange="shSet('browseable', this.checked)"> Im Netzwerk sichtbar (sonst nur über den direkten Pfad erreichbar)</label>
    <label class="row small"><input type="checkbox" ${d.recycle ? "checked" : ""} onchange="shSet('recycle', this.checked)"> Papierkorb – über die Freigabe gelöschte Dateien landen in <span class="mono">.nas-recycle</span></label>
    <label class="row small"><input type="checkbox" ${d.mac ? "checked" : ""} onchange="shSet('mac', this.checked)"> Für macOS optimieren</label>
    <label class="row small"><input type="checkbox" ${d.timemachine ? "checked" : ""} onchange="shSet('timemachine', this.checked)"> Als Time-Machine-Ziel verwenden</label>
    ${d.timemachine ? `<div class="field" style="max-width:260px"><label>Maximale Größe für Time Machine (GB, 0 = unbegrenzt)</label>
      <input type="number" min="0" value="${d.tm_size_gb || 0}" onchange="shSet('tm_size_gb', +this.value)"></div>` : ""}
    <label class="row small"><input type="checkbox" ${d.enabled ? "checked" : ""} onchange="shSet('enabled', this.checked)"> Freigabe aktiv</label>
    <div class="row" style="justify-content:flex-end;margin-top:14px"><button type="button" class="btn" onclick="$('#dlg').close()">Abbrechen</button>
      <button type="button" class="btn primary" id="sd-ok">Speichern</button></div>`;
  $("#sd-ok").onclick = async () => {
    try { await api("smb/share/save", sh.draft); $("#dlg").close(); toast("Freigabe gespeichert"); refresh(); }
    catch (e) { toast("Fehler: " + e.message); }
  };
}

// ---------------------------------------------------------------- Benutzer-Dialoge
window.userDialog = (preset) => {
  const p = preset || {};
  $("#dlg-body").innerHTML = `<h2>${p.ha ? "Benutzer aus Home Assistant" : "Neuer SMB-Benutzer"}</h2>
    <div class="settings-grid">
      <div class="field"><label>Benutzername</label><input type="text" id="ud-name" value="${esc(p.username || "")}" ${p.ha ? "readonly" : ""} autocomplete="off"></div>
      <div class="field"><label>Anzeigename</label><input type="text" id="ud-display" value="${esc(p.name || "")}"></div>
      <div class="field"><label>SMB-Passwort</label><input type="password" id="ud-pw" autocomplete="new-password" class="pw"></div>
      <div class="field"><label>Passwort wiederholen</label><input type="password" id="ud-pw2" autocomplete="new-password" class="pw"></div>
    </div>
    <div class="field"><label>Weitere Anmeldenamen <span class="muted small">(optional, durch Komma getrennt – z. B. die E-Mail-Adresse, mit der sich Geräte am alten Windows-Server anmelden)</span></label>
      <input type="text" id="ud-logins" value="${esc((p.logins || []).join(", "))}" placeholder="name@beispiel.de"></div>
    <p class="muted small">${p.ha ? "Home Assistant gibt sein Passwort nicht heraus – für Freigaben wird deshalb ein eigenes Passwort vergeben. Es darf gleich sein." : "Für Geräte ohne Home-Assistant-Konto, z. B. Fernseher oder Mediaplayer."}</p>
    <div class="row" style="justify-content:flex-end"><button type="button" class="btn" onclick="$('#dlg').close()">Abbrechen</button>
      <button type="button" class="btn primary" id="ud-ok">Anlegen</button></div>`;
  $("#ud-ok").onclick = async () => {
    if ($("#ud-pw").value !== $("#ud-pw2").value) { toast("Passwörter stimmen nicht überein"); return; }
    try {
      await api("smb/user/add", { name: $("#ud-name").value, display: $("#ud-display").value, password: $("#ud-pw").value, ha: !!p.ha,
        logins: $("#ud-logins").value.split(",").map((x) => x.trim()).filter(Boolean) });
      $("#dlg").close(); toast("Benutzer angelegt"); refresh();
    } catch (e) { toast("Fehler: " + e.message); }
  };
  if (!$("#dlg").open) $("#dlg").showModal();
};
window.pwDialog = (name) => {
  $("#dlg-body").innerHTML = `<h2>Passwort für ${esc(name)}</h2>
    <div class="field"><label>Neues SMB-Passwort</label><input type="password" id="pw1" class="pw" autocomplete="new-password"></div>
    <div class="field"><label>Wiederholen</label><input type="password" id="pw2" class="pw" autocomplete="new-password"></div>
    <div class="row" style="justify-content:flex-end"><button type="button" class="btn" onclick="$('#dlg').close()">Abbrechen</button>
      <button type="button" class="btn primary" id="pw-ok">Speichern</button></div>`;
  $("#pw-ok").onclick = async () => {
    if ($("#pw1").value !== $("#pw2").value) { toast("Passwörter stimmen nicht überein"); return; }
    try { await api("smb/user/password", { name, password: $("#pw1").value }); $("#dlg").close(); toast("Passwort geändert"); }
    catch (e) { toast("Fehler: " + e.message); }
  };
  $("#dlg").showModal();
};
window.importHaUsers = async () => {
  let list;
  try { list = await api("smb/ha_users"); } catch (e) { toast(e.message); return; }
  $("#dlg-body").innerHTML = `<h2>Benutzer aus Home Assistant</h2>
    <div class="picker">${list.length ? list.map((u, i) => `<div class="picker-row" data-i="${i}" ${u.valid ? "" : 'style="opacity:.5;cursor:default"'}>
      <span class="picker-name">👤 ${esc(u.name)}</span><span class="muted small mono">${esc(u.username)}</span><span>${u.valid ? "+" : "ungültiger Name"}</span></div>`).join("")
      : '<div class="muted small" style="padding:12px">Alle Home-Assistant-Benutzer sind bereits übernommen.</div>'}</div>
    <div class="row" style="justify-content:flex-end;margin-top:14px"><button type="button" class="btn" onclick="$('#dlg').close()">Schließen</button></div>`;
  document.querySelectorAll("#dlg [data-i]").forEach((r) => (r.onclick = () => {
    const u = list[+r.dataset.i]; if (!u.valid) return;
    userDialog({ username: u.username, name: u.name, ha: true });
  }));
  $("#dlg").showModal();
};

// ---------------------------------------------------------------- Servernamen, IP-Adressen, Anmeldenamen
window.addAlias = async () => {
  const name = $("#smb-alias").value.trim().toUpperCase();
  if (!name) return;
  try {
    const r = await api("smb/check_name", { name });
    if (r.taken_by.length && !confirm(`Der Name ${name} wird im Netzwerk noch von ${r.taken_by.join(", ")} verwendet (alter Server noch an?).\n\nTrotzdem hinzufügen? Es gibt dann einen Namenskonflikt, bis das andere Gerät aus ist.`)) return;
    await api("smb/config", { aliases: sh.st.config.aliases.concat([name]) });
    toast(`Auch erreichbar als \\\\${name}`);
  } catch (e) { toast("Fehler: " + e.message); }
  refresh();
};
window.removeAlias = (a) => act(() => api("smb/config", { aliases: sh.st.config.aliases.filter((x) => x !== a) }), "Name entfernt");
window.addIp = async () => {
  const ip = $("#smb-ip").value.trim();
  if (!ip) return;
  if (!confirm(`Home Assistant zusätzlich unter ${ip} erreichbar machen?\n\nDie Adresse darf von keinem anderen Gerät mehr benutzt werden – also erst, wenn der alte Server aus ist. Das wird vorher geprüft.`)) return;
  await act(() => api("smb/config", { extra_ips: sh.st.config.extra_ips.concat([ip]) }), "IP-Adresse hinzugefügt");
};
window.removeIp = (ip) => act(() => api("smb/config", { extra_ips: sh.st.config.extra_ips.filter((x) => x !== ip) }), "IP-Adresse entfernt");
window.loginsDialog = (name) => {
  const u = sh.st.users.find((x) => x.name === name) || {};
  $("#dlg-body").innerHTML = `<h2>Anmeldenamen für ${esc(u.display || name)}</h2>
    <p class="muted small">Unter diesen Namen kann sich ${esc(u.display || name)} zusätzlich anmelden – mit demselben SMB-Passwort.
    Praktisch, wenn Geräte die Anmeldung vom alten Windows-Server gespeichert haben (oft die E-Mail-Adresse des Microsoft-Kontos).</p>
    <div class="field"><label>Weitere Anmeldenamen (durch Komma getrennt)</label><input type="text" id="lg-in" value="${esc((u.logins || []).join(", "))}"></div>
    <div class="row" style="justify-content:flex-end"><button type="button" class="btn" onclick="$('#dlg').close()">Abbrechen</button>
      <button type="button" class="btn primary" id="lg-ok">Speichern</button></div>`;
  $("#lg-ok").onclick = () => { const logins = $("#lg-in").value.split(",").map((x) => x.trim()).filter(Boolean);
    $("#dlg").close(); act(() => api("smb/user/logins", { name, logins }), "Gespeichert"); };
  $("#dlg").showModal();
};

// ---------------------------------------------------------------- Übernahme vom alten Server
window.importDialog = () => {
  $("#dlg-body").innerHTML = `<h2>Freigaben vom alten Server übernehmen</h2>
    <p class="muted small">Liest die Freigaben (Namen, Beschreibung und – mit Administratorrechten – den Ordner) des alten Windows-Servers aus.
    Die Zugangsdaten werden nur für diesen Abruf verwendet und nicht gespeichert.</p>
    <div class="settings-grid">
      <div class="field"><label>Server (Name oder IP)</label><input type="text" id="im-host" placeholder="SERVER"></div>
      <div class="field"><label>Benutzer</label><input type="text" id="im-user" autocomplete="off" placeholder="name@beispiel.de"></div>
      <div class="field"><label>Passwort</label><input type="password" id="im-pw" class="pw" autocomplete="new-password"></div>
    </div>
    <div class="row" style="justify-content:flex-end;margin-top:10px"><button type="button" class="btn" onclick="$('#dlg').close()">Abbrechen</button>
      <button type="button" class="btn primary" id="im-load">Freigaben lesen</button></div>`;
  $("#im-load").onclick = async () => {
    const btn = $("#im-load"); btn.disabled = true; btn.textContent = "Verbinde …";
    try {
      const list = await api("smb/remote_shares", { host: $("#im-host").value, user: $("#im-user").value, password: $("#im-pw").value });
      $("#im-pw").value = "";
      importPick(list);
    } catch (e) { btn.disabled = false; btn.textContent = "Erneut versuchen"; toast("Fehler: " + e.message); }
  };
  $("#dlg").showModal();
};
function importPick(list) {
  const pools = state.status.pools;
  const users = sh.st.users;
  $("#dlg-body").innerHTML = `<h2>Freigaben übernehmen</h2>
    <p class="muted small">Prüfe für jede Freigabe den Ordner im Pool. Grün = Ordner im Pool gefunden. Der Laufwerksbuchstabe des alten Servers wird entfernt.</p>
    <div class="picker">${list.map((s, i) => `<div class="picker-row" style="grid-template-columns:auto 1fr;cursor:default;align-items:start">
      <input type="checkbox" data-im="${i}" ${s.exists ? "disabled" : s.found ? "checked" : ""}>
      <div><b>${esc(s.name)}</b> ${s.exists ? '<span class="badge">existiert schon</span>' : s.found ? '<span class="badge ok">Ordner gefunden</span>' : '<span class="badge warn">Ordner nicht gefunden</span>'}
        <div class="muted small">${s.path ? "alt: " + esc(s.path) : "Pfad nicht lesbar (keine Administratorrechte) – Ordner nach Namen vermutet"}${s.comment ? " · " + esc(s.comment) : ""}</div>
        <div class="row" style="margin-top:4px"><select data-pool="${i}" ${s.exists ? "disabled" : ""}>${pools.map((p) => `<option value="${esc(p.id)}" ${p.id === s.pool ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select>
          <input type="text" data-target="${i}" value="${esc(s.target)}" placeholder="(ganzer Pool)" ${s.exists ? "disabled" : ""} style="flex:1;min-width:160px"></div></div></div>`).join("")}</div>
    <div class="field" style="margin-top:10px"><label>Zugriff für die übernommenen Freigaben</label>
      ${users.length ? users.map((u) => `<label class="row small"><input type="checkbox" data-u="${esc(u.name)}" checked> ${esc(u.display || u.name)} – lesen + schreiben</label>`).join("")
        : '<span class="muted small">Noch keine Benutzer – Rechte danach pro Freigabe vergeben.</span>'}</div>
    <div class="row" style="justify-content:flex-end;margin-top:10px"><button type="button" class="btn" onclick="$('#dlg').close()">Abbrechen</button>
      <button type="button" class="btn primary" id="im-ok">Übernehmen</button></div>`;
  $("#im-ok").onclick = async () => {
    const items = [...document.querySelectorAll("[data-im]:checked")].map((x) => {
      const i = +x.dataset.im;
      return { name: list[i].name, comment: list[i].comment, pool: document.querySelector(`[data-pool="${i}"]`).value,
        target: document.querySelector(`[data-target="${i}"]`).value.trim().replace(/\\/g, "/").replace(/^\/+|\/+$/g, "") };
    });
    const access = {};
    document.querySelectorAll("[data-u]:checked").forEach((x) => (access[x.dataset.u] = "rw"));
    try {
      const r = await api("smb/import", { items, access });
      $("#dlg").close();
      toast(`${r.imported} Freigaben übernommen` + (r.skipped.length ? `, ${r.skipped.length} übersprungen` : ""));
    } catch (e) { toast("Fehler: " + e.message); }
    refresh();
  };
}

// ---------------------------------------------------------------- Direktfreigaben der Platten
function directCard(st) {
  const d = st.direct || { enabled: false, shares: [], access: {} };
  return `<div class="row spread" style="margin-top:8px"><h2>Direktfreigaben der Platten</h2>
      <label class="row small"><input type="checkbox" ${d.enabled ? "checked" : ""} onchange="setDirect({enabled:this.checked})"> eingeschaltet</label></div>
    <div class="card">
      <p class="muted small" style="margin-top:0">Jede Platte eines Pools zusätzlich als eigene Freigabe – mit dem kompletten Inhalt der Platte
        (PoolPart-Ordner und sonstige Dateien). Schaltbar auch in Home Assistant: <span class="mono">switch.raidpool_direktfreigaben</span> und ein Schalter pro Platte.
        Änderungen an Pool-Dateien über eine Direktfreigabe werden von der Duplizierung erkannt.</p>
      ${d.shares.length ? d.shares.map((x) => `<div class="row spread" style="padding:8px 0;border-top:1px solid var(--line)">
          <div><b>💽 ${esc(x.label)}</b> <span class="muted small">Pool ${esc(x.pool_name)}</span><br>
            <span class="mono small ${x.on ? "" : "muted"}">${esc(x.unc)}</span>${x.mounted ? "" : ' <span class="badge bad">nicht eingehängt</span>'}</div>
          <div class="row">${x.on ? `<button class="btn" style="padding:2px 8px" title="Kopieren" onclick='copyText(${js(x.unc)})'>⧉</button>` : ""}
            <label class="row small"><input type="checkbox" ${x.on ? "checked" : ""} onchange='setDirect({uuid:${js(x.uuid)}, on:this.checked})'> freigegeben</label></div></div>`).join("")
        : '<div class="muted small">Noch keine Platten in einem Pool.</div>'}
      <h3>Zugriff auf alle Direktfreigaben</h3>
      ${sh.st.users.length ? sh.st.users.map((u) => `<div class="row spread acc-row"><span>👤 ${esc(u.display || u.name)}</span>${accSeg(u.name, d.access[u.name] || "none", "dirAcc")}</div>`).join("")
        : '<div class="muted small">Noch keine Benutzer.</div>'}
      <label class="row small" style="margin-top:8px"><input type="checkbox" ${d.browseable !== false ? "checked" : ""} onchange="setDirect({browseable:this.checked})"> Im Netzwerk sichtbar</label>
    </div>`;
}
window.setDirect = (body) => act(() => api("smb/direct", body), "Gespeichert");
window.dirAcc = (name, a) => {
  const acc = Object.assign({}, (sh.st.direct || {}).access || {}); acc[name] = a;
  act(() => api("smb/direct", { access: acc }), "Zugriff gespeichert");
};
