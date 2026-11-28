// ---------------------------------------------------------------- Live-Ansicht (Übersicht + Duplizierung)
const live = { timer: null, open: true, data: null };
try { live.open = localStorage.getItem("raidpool.live") !== "0"; } catch (e) { /* egal */ }

function mbps(b) { return b >= 1e6 ? (b / 1e6).toFixed(b >= 1e8 ? 0 : 1) + " MB/s" : b >= 1e3 ? Math.round(b / 1e3) + " KB/s" : "0"; }
function meter(label, pct, sub = "") {
  const cls = pct >= 85 ? "bad" : pct >= 60 ? "warn" : "ok";
  return `<div class="lv-meter"><div class="row spread small"><span>${esc(label)}</span><b>${Math.round(pct)} %</b></div>
    <div class="lv-bar ${cls}"><div style="width:${Math.min(100, pct)}%"></div></div>${sub ? `<div class="small muted">${sub}</div>` : ""}</div>`;
}
function shortPath(p) { return p && p.length > 70 ? "…" + p.slice(-68) : p || ""; }

function liveHtml(d) {
  const s = d.system || {};
  const act = [];
  const r = d.rep || {};
  if (r.current) {
    const c = r.current; const pct = c.size ? (100 * c.done) / c.size : 0;
    act.push(`<div class="lv-act"><div class="row spread small"><span>📄 <b>${esc(c.action)}</b> ${esc(shortPath(c.path))}</span>
      <span class="muted">${fmtBytes(c.done)} / ${fmtBytes(c.size)} · ${mbps(r.speed)}</span></div>
      <div class="progress"><div style="width:${pct}%"></div></div></div>`);
  }
  if (d.copy_active) act.unshift(`<div class="lv-act small"><span class="badge info">Kopiervorgang erkannt</span> Scans, Abgleich und Duplikatsuche warten, bis er fertig ist – nur neue Dateien werden bearbeitet.</div>`);
  else if ((d.cache || []).length) act.unshift(`<div class="lv-act small"><span class="badge info">SSD-Cache wird verteilt</span> Platte-zu-Platte-Duplizierung, Scans und Abgleich warten so lange.</div>`);
  if (r.job) act.push(`<div class="lv-act small">🔁 <b>${esc(r.job.label)}</b> · ${esc(r.job.phase || "")}${r.job.total ? ` · ${fmtNum(r.job.done)} / ${fmtNum(r.job.total)}` : ""}${r.job.paused ? ` <span class="badge warn">${esc(r.job.paused)}</span>` : ""}</div>`);
  (d.cache || []).forEach((c) => act.push(`<div class="lv-act"><div class="row spread small"><span>⚡ <b>SSD-Cache → Platten</b> (${esc(c.pool)})${(c.active || []).length > 1 ? ` · ${c.active.length} Dateien gleichzeitig` : ""}</span>
      <span class="muted">${fmtNum(c.done)} / ${fmtNum(c.total)} · ${fmtBytes(c.bytes)}</span></div>
      <div class="progress"><div style="width:${c.total ? (100 * c.done) / c.total : 0}%"></div></div>
      ${((c.active && c.active.length) ? c.active : [c.current]).filter(Boolean).map((f) => `<div class="small muted">↳ ${esc(shortPath(f))}</div>`).join("")}</div>`));
  (d.scans || []).forEach((x) => act.push(`<div class="lv-act small">🔎 <b>${x.quick ? "Schneller Scan" : "Dateiliste einlesen"}</b> (${esc(x.pool)}) ·
      ${x.quick ? `${fmtNum(x.dirs)} Ordner geprüft · ` : ""}${fmtNum(x.files)} Dateien · Platte ${x.disks_done + (x.disks_done < x.disks_total ? 1 : 0)}/${x.disks_total}${d.scan_paused ? ' <span class="badge warn">pausiert</span>' : ""}</div>`));
  if (d.dupes) {
    const p = d.dupes; act.push(`<div class="lv-act"><div class="row spread small"><span>🧬 <b>Duplikatsuche</b> · ${esc(p.phase)}${p.paused ? ' <span class="badge warn">pausiert</span>' : ""}</span>
      <span class="muted">${fmtNum(p.done)} / ${fmtNum(p.total)} · ${fmtBytes(p.read_bytes)} gelesen</span></div>
      <div class="progress"><div style="width:${p.total ? (100 * p.done) / p.total : 0}%"></div></div></div>`);
  }
  if (d.deleting) {
    const x = d.deleting; act.push(`<div class="lv-act"><div class="row spread small"><span>🗑 <b>Duplikate löschen</b> · in den Papierkorb</span>
      <span class="muted">${fmtNum(x.done)} / ${fmtNum(x.total)} · ${fmtBytes(x.freed)} frei</span></div>
      <div class="progress"><div style="width:${x.total ? (100 * x.done) / x.total : 0}%"></div></div></div>`);
  }
  if (d.check) act.push(`<div class="lv-act small">🩺 <b>Dateisystem-Prüfung</b> ${esc(d.check.label)} · ${esc(d.check.phase || "")}</div>`);
  if (r.queue) act.push(`<div class="lv-act small">⏳ ${fmtNum(r.queue)} Datei(en) warten auf Duplizierung${(d.cache || []).length ? " – erst wenn der SSD-Cache verteilt ist" : ""}</div>`);

  const disks = (d.disks || []).map((x) => `<div class="lv-disk">
      <div class="row spread small"><span><b>${esc(x.label || x.model)}</b> <span class="muted">${esc(x.dev)}${x.system ? " · System" : ""}</span></span>
      <span class="muted">Lesen ${mbps(x.read)} · Schreiben ${mbps(x.write)} · <b>${Math.round(x.busy)} %</b></span></div>
      <div class="lv-bar ${x.busy >= 85 ? "bad" : x.busy >= 60 ? "warn" : "ok"}"><div style="width:${x.busy}%"></div></div></div>`).join("");

  const open = (d.smb_open || []).map((f) => `<div class="lv-file small"><span class="badge ${f.write ? "info" : ""}">${f.write ? "schreibt" : "liest"}</span> ${esc(shortPath(f.path))}</div>`).join("");
  const recent = (d.recent || []).map((f) => `<div class="lv-file small"><span class="muted">${new Date(f.t * 1000).toLocaleTimeString("de-DE")}</span>
      <span class="badge">${esc(f.what)}</span> ${esc(shortPath(f.path))}${f.size != null ? ` <span class="muted">${fmtBytes(f.size)}</span>` : ""}</div>`).join("");

  return `<details class="card lv" ${live.open ? "open" : ""} id="lv-details">
    <summary><b>Live</b> <span class="small muted">· ${act.length ? act.length + " Vorgang/Vorgänge aktiv" : "nichts aktiv"} · CPU ${Math.round(s.cpu || 0)} % · aktualisiert ${new Date(d.t * 1000).toLocaleTimeString("de-DE")}</span></summary>
    <div class="lv-grid">
      <div>
        <h3>Jetzt aktiv</h3>
        ${act.length ? act.join("") : '<div class="small muted">Gerade läuft nichts im Hintergrund.</div>'}
        <h3>Auslastung</h3>
        <div class="lv-meters">${meter("CPU gesamt", s.cpu || 0, s.cores ? s.cores + " Kerne" : "")}${meter("Arbeitsspeicher", s.mem || 0)}${meter("RAIDPool (Verwaltung)", s.self_cpu || 0)}</div>
        ${(s.procs || []).length ? `<div class="small muted" style="margin-top:6px">CPU je Programm im Add-on (100 % = ein Kern):<br>${s.procs.map((p) => `${esc(p.name)} <b>${p.cpu} %</b>`).join(" · ")}</div>` : ""}
      </div>
      <div>
        <h3>Platten</h3>
        ${disks || '<div class="small muted">Keine Daten.</div>'}
      </div>
    </div>
    <div class="lv-grid">
      <div><h3>Gerade geöffnet über Freigaben</h3>${open || '<div class="small muted">Keine Dateien geöffnet.</div>'}</div>
      <div><h3>Zuletzt geändert</h3>${recent || '<div class="small muted">Noch keine Änderungen seit dem Start.</div>'}</div>
    </div>
  </details>`;
}

async function liveTick() {
  clearTimeout(live.timer);
  const box = $("#live-box");
  const show = state.tab === "overview" || state.tab === "dup";
  box.hidden = !show;
  if (!show || document.hidden) { live.timer = setTimeout(liveTick, 4000); return; }
  try {
    live.data = await api("live");
    box.innerHTML = liveHtml(live.data);
    const det = $("#lv-details");
    det.ontoggle = () => { live.open = det.open; try { localStorage.setItem("raidpool.live", live.open ? "1" : "0"); } catch (e) { /* egal */ } };
  } catch (e) { /* Add-on kurz nicht erreichbar */ }
  live.timer = setTimeout(liveTick, live.open ? 2000 : 6000);
}
window.liveTick = liveTick;
setTimeout(liveTick, 500);
