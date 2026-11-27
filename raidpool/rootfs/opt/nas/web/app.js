"use strict";
// HA RAIDPool – Weboberfläche (läuft über HA-Ingress, daher nur relative URLs)

const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const state = { tab: "overview", status: null, disks: [], dup: { pool: null, path: "", mode: "direct" }, timer: null };

function fmtBytes(b) {
  if (b == null) return "–";
  const u = ["B", "KB", "MB", "GB", "TB", "PB"]; let i = 0; let v = Number(b);
  while (v >= 1000 && i < u.length - 1) { v /= 1000; i++; }
  return (i ? v.toFixed(v >= 100 ? 0 : 1) : v) + " " + u[i];
}
const js = (v) => esc(JSON.stringify(v));
const fmtNum = (n) => Number(n || 0).toLocaleString("de-DE");
const fmtTime = (t) => (t ? new Date(t * 1000).toLocaleString("de-DE") : "–");

async function api(path, body) {
  const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch("api/" + path, opt);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.classList.add("show"); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), 3500); }
async function act(fn, okMsg) {
  try { await fn(); if (okMsg) toast(okMsg); await refresh(); } catch (e) { toast("Fehler: " + e.message); }
}

function copiesBadge(min, max, files) {
  if (!files) return '<span class="muted">–</span>';
  if (min === max) return `<span class="badge ${min > 1 ? "dup" : ""}">${min}×</span>`;
  return `<span class="badge warn">${min}–${max}×</span>`;
}
function smartBadge(s) {
  if (!s) return '<span class="badge">SMART –</span>';
  if (!s.available) return '<span class="badge" title="' + esc(s.reason) + '">SMART n. v.</span>';
  const cls = { ok: "ok", warnung: "warn", fehler: "bad" }[s.state] || "";
  return `<span class="badge ${cls}">${esc(s.state === "ok" ? "SMART ok" : "SMART " + s.state)}</span>` + (s.standby ? ' <span class="badge">schläft</span>' : "");
}
function diskName(uuid) {
  for (const d of state.disks) for (const p of d.partitions) if (p.uuid === uuid) return (d.model || d.name) + (p.label ? " · " + p.label : "");
  return uuid.slice(0, 8);
}
function usageBar(sp) {
  const t = sp.total || 1;
  const w = (v) => Math.max(0, (100 * v) / t).toFixed(2) + "%";
  return `<div class="bar"><div style="width:${w(sp.pool_logical)};background:var(--seg-data)"></div>
    <div style="width:${w(sp.duplication_overhead)};background:var(--seg-dup)"></div>
    <div style="width:${w(sp.other)};background:var(--seg-other)"></div></div>
    <div class="legend"><span><i style="background:var(--seg-data)"></i>Daten ${fmtBytes(sp.pool_logical)}</span>
    <span><i style="background:var(--seg-dup)"></i>Duplikate ${fmtBytes(sp.duplication_overhead)}</span>
    <span><i style="background:var(--seg-other)"></i>Sonstiges ${fmtBytes(sp.other)}</span>
    <span><i style="background:var(--seg-free)"></i>Frei ${fmtBytes(sp.free)}</span></div>`;
}

// ---------------------------------------------------------------- Übersicht
function renderBanner() {
  const s = state.status; const el = $("#banner"); let html = "";
  if (s.options.read_only) html += `<div class="banner warn"><b>Schreibschutz aktiv.</b> Alle Platten und Pools werden nur lesend eingebunden – nichts auf den Platten wird verändert. Zum Schreiben in den Add-on-Optionen <code>read_only</code> ausschalten.</div>`;
  if (window.healthBanner) html += window.healthBanner();
  if (s.candidates.length) html += `<div class="banner row spread"><div><b>Bestehender DrivePool gefunden:</b> ${s.candidates.length} PoolPart-Ordner auf ${new Set(s.candidates.map((c) => c.uuid)).size} Platten.</div><button class="btn primary" onclick="openCreate(true)">Als Pool übernehmen</button></div>`;
  el.innerHTML = html;
}

function renderOverview() {
  const s = state.status; const el = $("#tab-overview");
  if (!s.pools.length) {
    el.innerHTML = `<div class="card empty">Noch kein Pool eingerichtet.<br><br><button class="btn primary" onclick="openCreate(${s.candidates.length > 0})">Pool anlegen</button></div>`;
    return;
  }
  const mq = s.mqtt || {};
  const integ = mq.connected ? `<div class="small muted" style="margin:-6px 0 10px">🔗 In Home Assistant eingebunden über MQTT · ${fmtNum(mq.entities)} Entitäten</div>`
    : `<div class="small muted" style="margin:-6px 0 10px">🔗 ${esc(mq.error || "Verbinde mit Home Assistant …")}</div>`;
  el.innerHTML = `<div class="row spread"><h2>Pools</h2><button class="btn" onclick="openCreate(false)">+ Pool</button></div>${integ}<div class="grid">` +
    s.pools.map((p) => {
      const sc = p.scan || {}; const running = sc.status === "läuft";
      const pct = p.space.total ? Math.round((100 * (p.space.total - p.space.free)) / p.space.total) : 0;
      const prog = running && sc.disks_total ? Math.round((100 * sc.disks_done) / sc.disks_total) : 0;
      return `<div class="card">
        <div class="row spread"><h2 style="margin:0">${esc(p.name)}</h2>
          <div class="row">${p.mounted ? '<span class="badge ok">eingehängt</span>' : '<span class="badge bad">nicht eingehängt</span>'}
          ${p.read_only ? '<span class="badge">nur lesen</span>' : ""}${p.degraded ? '<span class="badge bad">Platte fehlt</span>' : ""}
          ${p.adopted_drivepool ? '<span class="badge info">DrivePool</span>' : ""}</div></div>
        <div class="muted small">${fmtBytes(p.space.total)} gesamt · ${pct} % belegt · Standard-Duplizierung ${p.default_target}×</div>
        ${usageBar(p.space)}
        <div class="stats">
          <div class="stat"><b>${fmtNum(p.files)}</b><span>Dateien</span></div>
          <div class="stat"><b>${fmtNum(p.dup.c1)}</b><span>1× gespeichert</span></div>
          <div class="stat"><b>${fmtNum(p.dup.c2)}</b><span>2×</span></div>
          <div class="stat"><b>${fmtNum(p.dup.c3)}</b><span>3× und mehr</span></div>
          <div class="stat"><b style="color:${p.dup.under ? "var(--bad)" : "inherit"}">${fmtNum(p.dup.under)}</b><span>unter Soll</span></div>
          <div class="stat"><b style="color:${p.dup.inconsistent ? "var(--warn)" : "inherit"}">${fmtNum(p.dup.inconsistent)}</b><span>abweichende Kopien</span></div>
        </div>
        <div class="small muted">${running ? `Scan läuft: ${fmtNum(sc.files)} Dateien (${fmtBytes(sc.bytes)}), Platte ${sc.disks_done + 1}/${sc.disks_total}` :
          sc.status ? `Letzter Scan: ${fmtTime(sc.finished)} – ${esc(sc.status)}${sc.error ? ": " + esc(sc.error) : ""}` : "Noch nicht gescannt"}</div>
        ${running ? `<div class="progress"><div style="width:${prog}%"></div></div>` : ""}
        ${p.error ? `<ul class="warnings"><li>${esc(p.error)}</li></ul>` : ""}
        ${window.repPoolLine ? window.repPoolLine(p) : ""}
        <h3>Platten</h3>
        ${p.members.map((m) => {
          const u = m.usage; const w = u ? (100 * u.used) / u.total : 0;
          return `<div class="member"><div><b>${esc(m.model || m.disk || m.uuid)}</b> <span class="muted small">${esc(m.disk || "")} ${esc(m.label || "")}</span></div>
            <div>${m.available ? (m.smart_state ? smartBadge({ available: true, state: m.smart_state }) : "") : '<span class="badge bad">fehlt</span>'}</div>
            <div class="muted small">${u ? fmtBytes(u.free) + " frei von " + fmtBytes(u.total) : "–"}</div>
            <div>${!m.available ? `<button class="btn primary" style="padding:2px 8px;font-size:12px" onclick='memberDialog(${js(p.id)}, ${js(m.uuid)}, ${js(m.model || m.label || m.uuid)})'>Ersetzen …</button>`
              : p.members.length > 1 ? `<button class="btn" style="padding:2px 8px;font-size:12px" onclick='removeMember(${js(p.id)}, ${js(m.uuid)}, ${js(m.model || m.label || m.uuid)})'>Aus Pool nehmen</button>` : ""}</div>
            <div class="bar"><div style="width:${w}%;background:var(--seg-data)"></div></div></div>`;
        }).join("")}
        <div style="margin-top:6px"><button class="btn" style="font-size:12px;padding:3px 10px" onclick='memberDialog(${js(p.id)})'>+ Platte hinzufügen</button></div>
        <div class="row" style="margin-top:12px">
          <button class="btn primary" onclick="openDup('${p.id}')">Duplizierung</button>
          <button class="btn" onclick="repJob('abgleich','${p.id}')" title="Soll-Duplizierung jetzt vollständig herstellen">Jetzt abgleichen</button>
          <button class="btn" onclick="repJob('balance','${p.id}')" title="Füllstände der Platten angleichen">Ausbalancieren</button>
          <button class="btn" ${running ? "disabled" : ""} onclick="act(()=>api('pools/scan',{pool:'${p.id}'}),'Scan gestartet')">Neu scannen</button>
          <button class="btn" onclick="adopt('${p.id}')">Erkannte Duplizierung als Soll</button>
          ${p.mounted ? `<button class="btn" onclick="act(()=>api('pools/unmount',{pool:'${p.id}'}),'Ausgehängt')">Aushängen</button>`
            : `<button class="btn" onclick="act(()=>api('pools/mount',{pool:'${p.id}'}),'Eingehängt')">Einhängen</button>`}
          <button class="btn danger" onclick="delPool('${p.id}','${esc(p.name)}')">Entfernen</button>
        </div>
        <div class="muted small mono" style="margin-top:8px">${esc(p.mountpoint)}</div>
      </div>`;
    }).join("") + "</div>";
}

window.adopt = (pid) => {
  if (!confirm("Die aktuell erkannte Duplizierung (wie oft liegt jede Datei auf den Platten) wird als Soll-Einstellung pro Ordner übernommen. Bisherige Soll-Einstellungen dieses Pools werden ersetzt. Fortfahren?")) return;
  act(() => api("pools/adopt_targets", { pool: pid }), "Soll-Duplizierung übernommen");
};
window.delPool = (pid, name) => {
  if (!confirm(`Pool „${name}“ entfernen?\n\nEs werden KEINE Dateien gelöscht – nur die Pool-Einstellung und der Index. Die Dateien bleiben auf den Platten erhalten.`)) return;
  act(() => api("pools/delete", { pool: pid }), "Pool entfernt");
};

window.openCreate = async (adopt) => {
  await loadDisks();
  const cands = state.status.candidates;
  const used = new Set(state.status.pools.flatMap((p) => p.members.map((m) => m.uuid)));
  const empty = [];
  for (const d of state.disks) {
    if (d.system) continue;
    for (const p of d.partitions) if (p.mountpoint && !p.poolparts.length && !used.has(p.uuid)) empty.push({ d, p });
  }
  const ro = state.status.options.read_only;
  $("#dlg-body").innerHTML = `<h2>${adopt ? "DrivePool übernehmen" : "Neuen Pool anlegen"}</h2>
    <div class="field"><label>Name</label><input type="text" id="c-name" value="${adopt ? "DrivePool" : "Pool"}"></div>
    ${cands.length ? `<h3>Vorhandene DrivePool-Ordner</h3>` + cands.map((c, i) => `<label class="check"><input type="checkbox" data-c="${i}" ${adopt ? "checked" : ""}>
      <div><b>${esc(c.model || c.disk)}</b> <span class="muted small">${esc(c.disk)} ${esc(c.label)}</span><br>
      <span class="mono">${esc(c.poolpart)}</span><br><span class="muted small">${c.usage ? fmtBytes(c.usage.used) + " belegt von " + fmtBytes(c.usage.total) : ""}</span></div></label>`).join("") : ""}
    ${!adopt ? `<h3>Weitere Platten und Netzwerkfreigaben</h3>` + (empty.length ? empty.map(({ d, p }, i) => `<label class="check"><input type="checkbox" data-e="${i}">
      <div style="flex:1"><b>${d.network ? "🌐 " : ""}${esc(d.model || d.name)}</b> <span class="muted small">${esc(p.path)} · ${esc(p.fstype)} · ${fmtBytes(p.usage ? p.usage.total : p.size)}</span>
      <div style="margin-top:6px"><select data-m="${i}" onclick="event.stopPropagation()">
        <option value="pp" ${ro ? "disabled" : d.network ? "" : "selected"}>Eigenen PoolPart-Ordner anlegen${ro ? " (nur mit Schreibzugriff)" : ""}</option>
        <option value="direct" ${ro || d.network ? "selected" : ""}>Vorhandenen Inhalt direkt einbinden</option></select></div>
      <span class="muted small">PoolPart-Ordner: Pool-Dateien bleiben getrennt von anderen Daten. Direkt: alle Dateien der ${d.network ? "Freigabe" : "Platte"} erscheinen im Pool (z. B. externes NAS).</span></div></label>`).join("")
      : '<div class="muted small">Keine weiteren eingehängten Platten oder Freigaben.</div>') : ""}
    <div class="field"><label>Standard-Duplizierung</label><select id="c-def"><option value="1">1× (keine)</option><option value="2">2×</option><option value="3">3×</option></select>
    <span class="muted small">Gilt für alle Ordner ohne eigene Einstellung. Bei DrivePool-Übernahme danach „Erkannte Duplizierung als Soll“ nutzen.</span></div>
    <div class="row" style="justify-content:flex-end;margin-top:14px"><button class="btn" value="cancel">Abbrechen</button><button class="btn primary" id="c-ok" value="ok">Anlegen</button></div>`;
  $("#c-ok").onclick = (ev) => {
    ev.preventDefault();
    const members = [...document.querySelectorAll("[data-c]:checked")].map((x) => ({ uuid: cands[x.dataset.c].uuid, poolpart: cands[x.dataset.c].poolpart }))
      .concat([...document.querySelectorAll("[data-e]:checked")].map((x) => ({ uuid: empty[x.dataset.e].p.uuid,
        poolpart: document.querySelector(`[data-m="${x.dataset.e}"]`).value === "direct" ? "." : "" })));
    $("#dlg").close();
    act(() => api("pools/create", { name: $("#c-name").value, members, default_target: +$("#c-def").value, adopt }), "Pool angelegt – Scan läuft");
  };
  $("#dlg").showModal();
};

// ---------------------------------------------------------------- Platten
const POWER_LABEL = { always: "Immer an (Dauerbetrieb)", vendor: "Herstellervorgabe", spindown: "Abschalten" };
function powerSelect(d) {
  const p = d.power || {}; const def = state.status.options;
  const defLabel = def.power_default === "spindown" ? `Abschalten nach ${def.spindown_minutes} min` : POWER_LABEL[def.power_default];
  const cur = p.inherited ? "default" : p.mode === "spindown" ? "spindown:" + p.minutes : p.mode;
  const opts = [["default", `Standard (${defLabel})`], ["always", POWER_LABEL.always], ["vendor", POWER_LABEL.vendor],
    ...[30, 60, 120, 180, 300].map((m) => ["spindown:" + m, `Abschalten nach ${m >= 60 ? m / 60 + " h" : m + " min"}`])];
  if (cur.startsWith("spindown:") && !opts.some((o) => o[0] === cur)) opts.push([cur, `Abschalten nach ${p.minutes} min`]);
  return `<div class="row spread" style="margin-top:10px"><span class="small muted">Energie</span>
    <select onchange='setPower(${js(p.key)}, this.value)'>${opts.map(([v, l]) => `<option value="${v}" ${v === cur ? "selected" : ""}>${esc(l)}</option>`).join("")}</select></div>
    ${p.mode === "always" ? '<div class="muted small">Kein Standby, Kopfparken aus, Platte wird alle 3 min kurz gelesen.</div>' : ""}
    ${p.error ? `<div class="muted small">Hinweis: ${esc(p.error)}${p.mode === "always" ? " – Wachhalten durch Lesen ist aktiv." : ""}</div>` : ""}`;
}
window.setPower = (key, v) => {
  const [mode, minutes] = v.split(":");
  act(() => api("disks/power", { key, mode, minutes: minutes ? +minutes : undefined }), "Energieeinstellung gespeichert");
};
async function loadDisks() { state.disks = await api("disks"); }
function netCard(d) {
  const p = d.partitions[0]; const s = d.share; const u = p.usage;
  return `<div class="card"><div class="row spread"><b>🌐 ${esc(s.name)}</b><div class="row">
      <span class="badge info">${s.kind.toUpperCase()}</span>${p.mountpoint ? '<span class="badge ok">verbunden</span>' : '<span class="badge bad">getrennt</span>'}</div></div>
    <div class="muted small mono">${esc(d.path)}${s.username ? " · " + esc(s.username) : " · Gast"}</div>
    ${u ? `<div class="bar"><div style="width:${(100 * u.used) / u.total}%;background:var(--seg-data)"></div></div>
      <div class="muted small">${fmtBytes(u.used)} belegt · ${fmtBytes(u.free)} frei von ${fmtBytes(u.total)}</div>` : ""}
    <div class="row" style="margin-top:6px">${p.pool ? `<span class="badge info">Pool ${esc(p.pool)}</span>` : ""}${p.poolparts.length && !p.pool ? '<span class="badge dup">DrivePool</span>' : ""}
      ${p.poolparts.map((x) => `<span class="mono small muted">${esc(x)}</span>`).join(" ")}</div>
    ${p.error ? `<ul class="warnings"><li>${esc(p.error)}</li></ul>` : ""}
    <div class="row" style="margin-top:10px">
      ${p.mountpoint ? `<button class="btn" onclick="act(()=>api('disks/unmount',{uuid:'${p.uuid}'}),'Getrennt')">Trennen</button>`
        : `<button class="btn primary" onclick="act(()=>api('disks/mount',{uuid:'${p.uuid}'}),'Verbunden')">Verbinden</button>`}
      <button class="btn" onclick='openNetShare(${js(s.id)})' ${p.mountpoint ? 'disabled title="Zum Bearbeiten zuerst trennen"' : ""}>Bearbeiten</button>
      <button class="btn danger" onclick='removeNetShare(${js(s.id)}, ${js(s.name)})'>Entfernen</button></div>
  </div>`;
}
window.removeNetShare = (id, name) => {
  if (!confirm(`Netzwerkfreigabe „${name}“ entfernen?\n\nAuf dem Server wird nichts gelöscht.`)) return;
  act(() => api("net/remove", { id }), "Freigabe entfernt");
};
window.openNetShare = (id) => {
  const s = id ? (state.disks.find((d) => d.network && d.share.id === id) || {}).share : null;
  const v = s || { kind: "smb", automount: true };
  $("#dlg-body").innerHTML = `<h2>${s ? "Netzwerkfreigabe bearbeiten" : "Netzwerkfreigabe hinzufügen"}</h2>
    <div class="field"><label>Typ</label><div class="seg" id="ns-kind">
      <button type="button" data-k="smb" class="${v.kind === "smb" ? "active" : ""}">SMB / Windows / NAS</button>
      <button type="button" data-k="nfs" class="${v.kind === "nfs" ? "active" : ""}">NFS</button></div></div>
    <div class="settings-grid">
      <div class="field"><label>Name</label><input type="text" id="ns-name" value="${esc(v.name || "")}" placeholder="z. B. Synology Filme"></div>
      <div class="field"><label>Server (Name oder IP)</label><input type="text" id="ns-server" value="${esc(v.server || "")}" placeholder="192.168.0.20"></div>
      <div class="field"><label id="ns-share-l">${v.kind === "nfs" ? "Export-Pfad" : "Freigabe"}</label><input type="text" id="ns-share" value="${esc(v.share || "")}" placeholder="${v.kind === "nfs" ? "volume1/filme" : "Filme"}"></div>
    </div>
    <div class="settings-grid" id="ns-auth" ${v.kind === "nfs" ? "hidden" : ""}>
      <div class="field"><label>Benutzer <span class="muted small">(leer = Gast)</span></label><input type="text" id="ns-user" value="${esc(v.username || "")}" autocomplete="off"></div>
      <div class="field"><label>Passwort</label><input type="password" id="ns-pass" placeholder="${s ? "unverändert lassen" : ""}" autocomplete="new-password"
        style="font:inherit;padding:6px 8px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg)"></div>
      <div class="field"><label>Domäne <span class="muted small">(optional)</span></label><input type="text" id="ns-domain" value="${esc(v.domain || "")}"></div>
    </div>
    <div class="field"><label>Protokollversion</label><select id="ns-ver"></select></div>
    <label class="row small"><input type="checkbox" id="ns-auto" ${v.automount !== false ? "checked" : ""}> Beim Start automatisch verbinden</label>
    <p class="muted small">Die Freigabe wird ${state.status.options.read_only ? "<b>nur lesend</b> (read_only aktiv)" : "mit Schreibzugriff"} eingebunden. Das Passwort wird getrennt und nicht im sichtbaren Konfigurationsordner gespeichert.</p>
    <div class="row" style="justify-content:flex-end;margin-top:10px"><button type="button" class="btn" id="ns-cancel">Abbrechen</button>
      <button type="button" class="btn primary" id="ns-ok">${s ? "Speichern und verbinden" : "Hinzufügen und verbinden"}</button></div>`;
  let kind = v.kind;
  const vers = () => {
    const list = kind === "smb" ? [["", "Automatisch"], ["3.1.1", "SMB 3.1.1"], ["3.0", "SMB 3.0"], ["2.1", "SMB 2.1"], ["1.0", "SMB 1 (alt, unsicher)"]]
      : [["", "Automatisch"], ["4.2", "NFS 4.2"], ["4.1", "NFS 4.1"], ["4", "NFS 4"], ["3", "NFS 3"]];
    $("#ns-ver").innerHTML = list.map(([x, l]) => `<option value="${x}" ${x === (v.kind === kind ? v.version || "" : "") ? "selected" : ""}>${l}</option>`).join("");
  };
  vers();
  document.querySelectorAll("#ns-kind button").forEach((b) => (b.onclick = () => {
    kind = b.dataset.k;
    document.querySelectorAll("#ns-kind button").forEach((x) => x.classList.toggle("active", x === b));
    $("#ns-auth").hidden = kind === "nfs";
    $("#ns-share-l").textContent = kind === "nfs" ? "Export-Pfad" : "Freigabe";
    $("#ns-share").placeholder = kind === "nfs" ? "volume1/filme" : "Filme";
    vers();
  }));
  $("#ns-cancel").onclick = () => $("#dlg").close();
  $("#ns-ok").onclick = async () => {
    const body = { id: s ? s.id : undefined, kind, name: $("#ns-name").value, server: $("#ns-server").value, share: $("#ns-share").value,
      username: kind === "smb" ? $("#ns-user").value : "", password: kind === "smb" ? $("#ns-pass").value : "",
      domain: kind === "smb" ? $("#ns-domain").value : "", version: $("#ns-ver").value, automount: $("#ns-auto").checked };
    const btn = $("#ns-ok"); btn.disabled = true; btn.textContent = "Verbinde …";
    try { await api("net/save", body); $("#dlg").close(); toast("Netzwerkfreigabe verbunden"); }
    catch (e) { btn.disabled = false; btn.textContent = "Erneut versuchen"; toast("Fehler: " + e.message); if (e.message.startsWith("Gespeichert")) $("#dlg").close(); }
    refresh();
  };
  $("#dlg").showModal();
};

function renderDisks() {
  const el = $("#tab-disks");
  const nets = state.disks.filter((d) => d.network);
  const locals = state.disks.filter((d) => !d.network);
  el.innerHTML = (window.healthTop ? window.healthTop() : "") + `<div class="row spread"><h2>Netzwerkfreigaben</h2><button class="btn" onclick="openNetShare()">+ Netzwerkfreigabe</button></div>
    ${nets.length ? `<div class="grid">${nets.map(netCard).join("")}</div>` : '<div class="card muted small">Noch keine Netzwerkfreigabe eingebunden. Freigaben eines anderen NAS (SMB oder NFS) können wie eine Platte in einen Pool aufgenommen oder nach Duplikaten durchsucht werden.</div>'}
    <div class="row spread" style="margin-top:8px"><h2>Platten</h2><div class="row">
      <button class="btn" onclick="act(()=>api('smart/refresh',{}),'SMART aktualisiert')">SMART aktualisieren</button>
      <button class="btn" onclick="if(confirm('Auch schlafende Platten aufwecken?'))act(()=>api('smart/refresh',{wake:true}),'SMART aktualisiert')">inkl. schlafender</button></div></div>
    <div class="grid">` + locals.map((d) => {
      const s = d.smart;
      return `<div class="card"><div class="row spread"><b>${esc(d.model || d.name)}</b><div class="row">${d.system ? '<span class="badge info">System</span>' : ""}${smartBadge(s)}</div></div>
        <div class="muted small">${esc(d.path)} · ${fmtBytes(d.size)} · ${d.rotational ? "HDD" : "SSD"}${d.transport ? " · " + esc(d.transport.toUpperCase()) : ""}${d.serial ? " · SN " + esc(d.serial) : ""}</div>
        ${s && s.available ? `<div class="stats"><div class="stat"><b>${s.temperature ?? "–"} °C</b><span>Temperatur</span></div>
          <div class="stat"><b>${fmtNum(s.power_on_hours)}</b><span>Betriebsstunden</span></div>
          ${s.nvme ? `<div class="stat"><b>${s.nvme.percentage_used ?? "–"} %</b><span>Verschleiß</span></div>` : `<div class="stat"><b>${fmtNum(s.power_cycles)}</b><span>Einschaltvorgänge</span></div>`}
          ${s.start_stop != null ? `<div class="stat"><b>${fmtNum(s.start_stop)}</b><span>Anläufe (Motor)</span></div>` : ""}
          ${s.load_cycles != null ? `<div class="stat"><b style="color:${s.load_cycles > 300000 ? "var(--bad)" : "inherit"}">${fmtNum(s.load_cycles)}</b><span>Kopf-Parkvorgänge</span></div>` : ""}</div>` : ""}
        ${d.system ? "" : powerSelect(d)}
        ${d.system || !window.smartTestLine ? "" : window.smartTestLine(d)}
        ${s && !s.available ? `<div class="muted small">SMART nicht lesbar${s.reason ? ": " + esc(s.reason) : ""}. Unter Proxmox den SATA-Controller per PCIe durchreichen, nicht nur die einzelne Platte.</div>` : ""}
        ${s && s.warnings && s.warnings.length ? `<ul class="warnings">${s.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : ""}
        ${s && s.attributes && s.attributes.length ? `<details><summary class="small muted">Alle SMART-Attribute</summary><div class="table-wrap"><table class="small"><tr><th>ID</th><th>Attribut</th><th class="num">Wert</th><th class="num">Schwelle</th><th class="num">Rohwert</th></tr>
          ${s.attributes.map((a) => `<tr><td>${a.id}</td><td>${esc(a.name)}</td><td class="num">${a.value}</td><td class="num">${a.thresh}</td><td class="num">${fmtNum(a.raw)}</td></tr>`).join("")}</table></div></details>` : ""}
        <h3>Partitionen</h3>
        ${d.partitions.map((p) => `<div class="member"><div><b>${esc(p.label || p.name)}</b> <span class="muted small">${esc(p.fstype || "?")} · ${fmtBytes(p.size)}</span>
            ${p.pool ? `<span class="badge info">Pool ${esc(p.pool)}</span>` : ""}${p.poolparts.length && !p.pool ? '<span class="badge dup">DrivePool</span>' : ""}</div>
          <div>${d.system ? "" : p.mountpoint ? `<span class="badge ok">${p.mount_ro === false ? "rw" : "ro"}</span> <button class="btn" onclick="act(()=>api('disks/unmount',{uuid:'${p.uuid}'}),'Ausgehängt')">Aushängen</button>`
            : `<button class="btn" onclick="act(()=>api('disks/mount',{uuid:'${p.uuid}'}),'Eingehängt')">Einhängen</button>`}</div>
          <div class="muted small" style="grid-column:1/-1">${p.usage ? fmtBytes(p.usage.used) + " belegt, " + fmtBytes(p.usage.free) + " frei" : ""}
            ${p.poolparts.map((x) => `<div class="mono">${esc(x)}</div>`).join("")}${p.error ? `<ul class="warnings"><li>${esc(p.error)}</li></ul>` : ""}
            ${!d.system && window.fsckLine ? window.fsckLine(d, p) : ""}</div></div>`).join("") || '<div class="muted small">Keine Partitionen</div>'}
      </div>`;
    }).join("") + "</div>";
}

// ---------------------------------------------------------------- Duplizierung
window.openDup = (pid) => { state.dup = { pool: pid, path: "", mode: "direct" }; switchTab("dup"); };
window.goDir = (path) => { state.dup.path = path; state.dup.mode = "direct"; renderDup(); };
window.setMode = (m) => { state.dup.mode = m; renderDup(); };
window.setTarget = (path, value) => act(() => api("pools/target", { pool: state.dup.pool, path, value }), "Soll-Duplizierung gespeichert");

async function renderDup() {
  const el = $("#tab-dup"); const pools = state.status.pools;
  if (!pools.length) { el.innerHTML = '<div class="card empty">Noch kein Pool vorhanden.</div>'; return; }
  if (!state.dup.pool || !pools.find((p) => p.id === state.dup.pool)) state.dup.pool = pools[0].id;
  const pool = pools.find((p) => p.id === state.dup.pool); const d = state.dup;
  let data, files;
  try {
    [data, files] = await Promise.all([api(`folders?pool=${encodeURIComponent(d.pool)}&path=${encodeURIComponent(d.path)}`),
      api(`files?pool=${encodeURIComponent(d.pool)}&path=${encodeURIComponent(d.path)}&mode=${d.mode}&limit=500`)]);
  } catch (e) { el.innerHTML = `<div class="card">Fehler: ${esc(e.message)}</div>`; return; }
  const f = data.folder; const n = data.members;
  const parts = d.path ? d.path.split("/") : [];
  const crumbs = [`<a onclick="goDir('')">${esc(pool.name)}</a>`].concat(parts.map((x, i) => `<a onclick='goDir(${js(parts.slice(0, i + 1).join("/"))})'>${esc(x)}</a>`)).join(" / ");
  const opts = (cur, inheritedVal, allowInherit) => (allowInherit ? `<option value="" ${cur == null ? "selected" : ""}>erben (${inheritedVal}×)</option>` : "") +
    Array.from({ length: n }, (_, i) => i + 1).map((v) => `<option value="${v}" ${cur === v ? "selected" : ""}>${v}×</option>`).join("");
  const targetSel = d.path
    ? `<select onchange='setTarget(${js(d.path)}, this.value)'>${opts(f ? f.target : null, f ? f.inherited_target : pool.default_target, true)}</select>`
    : `<select onchange="act(()=>api('pools/default_target',{pool:'${d.pool}',value:+this.value}),'Gespeichert')">${opts(pool.default_target, 1, false)}</select>`;
  el.innerHTML = (window.repCard ? window.repCard() : "") + `<div class="card">
    <div class="row spread"><div class="row"><select onchange="openDup(this.value)">${pools.map((p) => `<option value="${p.id}" ${p.id === d.pool ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select></div>
      <div class="row small muted">Soll-Duplizierung ${d.path ? "dieses Ordners" : "(Standard)"}: ${targetSel}</div></div>
    <div class="crumbs" style="margin-top:12px">${crumbs}</div>
    ${f ? `<div class="stats"><div class="stat"><b>${fmtNum(f.files)}</b><span>Dateien</span></div><div class="stat"><b>${fmtBytes(f.bytes)}</b><span>Daten</span></div>
      <div class="stat"><b>${fmtBytes(f.stored)}</b><span>belegt inkl. Kopien</span></div><div class="stat"><b>${copiesBadge(f.min_c, f.max_c, f.files)}</b><span>Ist</span></div>
      <div class="stat"><b>${f.effective_target}×</b><span>Soll</span></div><div class="stat"><b style="color:${f.under ? "var(--bad)" : "inherit"}">${fmtNum(f.under)}</b><span>unter Soll</span></div></div>`
      : '<div class="muted">Keine Daten – Pool noch nicht gescannt?</div>'}
    ${data.children.length ? `<div class="table-wrap"><table><tr><th>Ordner</th><th class="num">Dateien</th><th class="num">Größe</th><th>Ist</th><th>Soll</th><th class="num">unter Soll</th><th class="num">abweichend</th></tr>
      ${data.children.map((c) => `<tr class="click" onclick='goDir(${js(c.path)})'><td>📁 ${esc(c.name)}</td><td class="num">${fmtNum(c.files)}</td><td class="num">${fmtBytes(c.bytes)}</td>
        <td>${copiesBadge(c.min_c, c.max_c, c.files)}</td><td>${c.effective_target}×${c.target != null ? ' <span class="badge info">eigene</span>' : ""}</td>
        <td class="num" style="color:${c.under ? "var(--bad)" : "inherit"}">${fmtNum(c.under)}</td><td class="num">${c.inconsistent ? `<span class="badge warn">${fmtNum(c.inconsistent)}</span>` : "0"}</td></tr>`).join("")}</table></div>` : ""}
  </div>
  <div class="card"><div class="pills">${[["direct", "Dateien hier"], ["under", "unter Soll"], ["single", "nur 1×"], ["multi", "dupliziert"], ["inconsistent", "abweichende Kopien"]]
    .map(([m, l]) => `<button class="${d.mode === m ? "active" : ""}" onclick="setMode('${m}')">${l}</button>`).join("")}</div>
    ${files.length ? `<div class="table-wrap"><table><tr><th>Datei</th><th class="num">Größe</th><th>Kopien</th><th>auf Platten</th></tr>
      ${files.map((x) => `<tr><td class="mono">${esc(d.mode === "direct" ? x.path.split("/").pop() : x.path)}</td><td class="num">${fmtBytes(x.size)}</td>
        <td><span class="badge ${x.copies > 1 ? "dup" : ""}">${x.copies}×</span>${x.target ? ` <span class="muted small">Soll ${x.target}×</span>` : ""}${x.consistent ? "" : ` <span class="badge warn">abweichend</span> <button class="btn" style="padding:1px 8px;font-size:12px" onclick='resolveDialog(${js(d.pool)}, ${js(x.path)})'>Lösen …</button>`}</td>
        <td class="small">${x.disks.split(",").map((u) => esc(diskName(u))).join("<br>")}</td></tr>`).join("")}</table></div>
      ${files.length >= 500 ? '<div class="muted small">Es werden die ersten 500 Einträge angezeigt.</div>' : ""}` : '<div class="muted small">Keine Dateien in dieser Ansicht.</div>'}
  </div>`;
}

// ---------------------------------------------------------------- Ablauf
function switchTab(tab) {
  state.tab = tab;
  if (tab !== "dupes") $("#selbar").hidden = true;
  document.querySelectorAll("nav button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  document.querySelectorAll("main > section").forEach((s) => (s.hidden = s.id !== "tab-" + tab));
  refresh();
}
document.querySelectorAll("nav button").forEach((b) => (b.onclick = () => switchTab(b.dataset.tab)));

async function refresh() {
  try {
    state.status = await api("status");
    state.rep = await api("rep/status").catch(() => null);
    state.health = await api("health/status").catch(() => null);
    if (!state.disks.length || state.tab === "disks") await loadDisks();
  } catch (e) { $("#banner").innerHTML = `<div class="banner warn">Keine Verbindung zum Add-on: ${esc(e.message)}</div>`; return; }
  renderBanner();
  if (state.tab === "overview") renderOverview();
  else if (state.tab === "disks") renderDisks();
  else if (state.tab === "dup") await renderDup();
  else if (state.tab === "shares") await window.renderShares();
  else await window.renderDupes();
  const scanning = (state.health && state.health.check && state.health.check.running) || state.status.pools.some((p) => p.scan && p.scan.status === "läuft") || (state.rep && ((state.rep.job && state.rep.job.running) || state.rep.current || state.rep.queue)) || (state.tab === "dupes" && window.dupesRunning && window.dupesRunning());
  clearTimeout(state.timer);
  state.timer = setTimeout(refresh, scanning ? 3000 : 30000);
}
window.act = act; window.api = api;
refresh();
