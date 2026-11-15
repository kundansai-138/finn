// FINN: mobile-first investor agent. Preact + htm, no build step.
import { html, render, useState, useEffect, useRef, useMemo, useCallback } from "/static/vendor/preact-htm.module.js";
import { disablePush, enablePush, haptic, pushState, registerSW, useInstall, useOnline, usePullToRefresh, useSheetDrag, useSwipe } from "/static/mobile.js";

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------
const api = async (path, opts) => {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = `${r.status}`;
    try { msg = (await r.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return r.json();
};
const inr = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });
const inr1 = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 1 });
const cr = (v) => (v == null ? "–" : `₹${inr.format(v)} cr`);
const num = (v, d = 0) => (v == null ? "–" : (d ? inr1 : inr).format(v));
const sgn = (v, d = 0, unit = "%") => (v == null ? "–" : `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(d)}${unit}`);
const tone = (v) => (v == null ? "muted" : v > 0 ? "pos" : v < 0 ? "neg" : "muted");
const ago = (iso) => {
  if (!iso) return "";
  const d = new Date(iso.length <= 10 ? iso + "T15:30:00+05:30" : iso);
  const s = (Date.now() - d.getTime()) / 1000;
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  if (s < 86400 * 7) return `${Math.round(s / 86400)}d ago`;
  return d.toLocaleDateString("en-IN", { day: "numeric", month: "short" });
};
const go = (path) => { location.hash = path; };
const strengthBars = (s) => (s >= 0.6 ? 3 : s >= 0.35 ? 2 : 1);

const Icon = {
  brief: html`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M4 6h16M4 12h10M4 18h7"/></svg>`,
  portfolio: html`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M3 17l5-6 4 3 6-8 3 4"/><path d="M3 21h18"/></svg>`,
  network: html`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="6" cy="7" r="2.5"/><circle cx="18" cy="6" r="2.5"/><circle cx="12" cy="17" r="2.5"/><path d="M8 8.5l2.6 6.4M16.2 7.9l-2.8 7M8.4 7l7.1-.6"/></svg>`,
  arrow: html`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M5 12h13M13 6l6 6-6 6"/></svg>`,
  back: html`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M15 5l-7 7 7 7"/></svg>`,
  sliders: html`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2"/><circle cx="10" cy="17" r="2"/></svg>`,
  refresh: html`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M20 11a8 8 0 10-2.3 5.7M20 5v6h-6"/></svg>`,
  plus: html`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 5v14M5 12h14"/></svg>`,
};

// ---------------------------------------------------------------------------
// Charts (hand-rolled SVG: small, crisp, themeable)
// ---------------------------------------------------------------------------
function Spark({ data, w = 64, h = 22, mark = true }) {
  const v = (data || []).filter((x) => x != null);
  if (v.length < 2) return null;
  const lo = Math.min(...v), hi = Math.max(...v), rng = hi - lo || 1;
  const pts = v.map((y, i) => [(i / (v.length - 1)) * (w - 4) + 2, h - 3 - ((y - lo) / rng) * (h - 6)]);
  const up = v[v.length - 1] >= v[v.length - 2];
  const d = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join("");
  const last = pts[pts.length - 1];
  return html`<svg width=${w} height=${h} viewBox="0 0 ${w} ${h}" aria-hidden="true">
    <path d=${d} fill="none" stroke="var(--ink-3)" stroke-width="1.3" stroke-linejoin="round" />
    ${mark && html`<circle cx=${last[0]} cy=${last[1]} r="2.6" fill=${up ? "var(--pos)" : "var(--neg)"} />`}
  </svg>`;
}

// Charts draw at the container's real pixel width (not a scaled viewBox), so an 11px label is
// 11px on a 320px phone too.
function useWidth(fallback = 340) {
  const ref = useRef(null);
  const [w, setW] = useState(fallback);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(([e]) => setW(Math.max(240, Math.round(e.contentRect.width))));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, w];
}

function BarLine({ labels, bars, line, barLabel = "Revenue", lineLabel = "Margin %", h = 120 }) {
  const [ref, W] = useWidth();
  const n = labels.length;
  if (!n) return null;
  const pad = 18, bw = Math.max(6, (W - pad * 2) / n - 6);
  const bv = bars.map((x) => x ?? 0);
  const bmax = Math.max(...bv, 1);
  const lv = (line || []).filter((x) => x != null);
  // Pad the margin axis to >= 6 points so a 0.5pt wiggle doesn't read as a cliff.
  let lmin = lv.length ? Math.min(...lv) : 0, lmax = lv.length ? Math.max(...lv) : 1;
  if (lmax - lmin < 6) { const mid = (lmax + lmin) / 2; lmin = mid - 3; lmax = mid + 3; }
  const lr = lmax - lmin;
  const x = (i) => pad + i * ((W - pad * 2) / n) + 3;
  const pts = (line || []).map((y, i) => (y == null ? null : [x(i) + bw / 2, 14 + (1 - (y - lmin) / lr) * (h - 44)]));
  const path = pts.filter(Boolean).map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join("");
  return html`<div ref=${ref}>
    <svg class="chart" width=${W} height=${h} viewBox="0 0 ${W} ${h}" role="img" aria-label="${barLabel} and ${lineLabel}">
      ${bv.map((b, i) => {
        const bh = (b / bmax) * (h - 34);
        return html`<rect x=${x(i)} y=${h - 18 - bh} width=${bw} height=${Math.max(bh, 1)} rx="2" fill=${i === n - 1 ? "var(--ink)" : "var(--line-2)"} />`;
      })}
      ${path && html`<path d=${path} fill="none" stroke="var(--accent)" stroke-width="1.6" />`}
      ${pts.map((p, i) => p && (i === pts.length - 1 || i === 0) && html`<text x=${p[0]} y=${p[1] - 5} text-anchor="middle" style="fill:var(--accent)">${line[i].toFixed(1)}</text>`)}
      ${labels.map((l, i) => (i % Math.ceil(n / (W < 330 ? 4 : 6)) === 0 || i === n - 1) && html`<text x=${x(i) + bw / 2} y=${h - 5} text-anchor="middle">${shortPeriod(l)}</text>`)}
    </svg>
    <div class="legend"><span><i style="background:var(--line-2)"></i>${barLabel}</span>${line && html`<span><i style="background:var(--accent)"></i>${lineLabel}</span>`}</div>
  </div>`;
}
const shortPeriod = (p) => (p || "").replace(/^(\w{3}) (\d{2})(\d{2})$/, "$1 '$3").replace(/^Mar '(\d{2})$/, "FY$1");

// ---------------------------------------------------------------------------
// Shared bits
// ---------------------------------------------------------------------------
const Avatar = ({ p, cls = "" }) => html`<div class="avatar ${cls}">${p?.initials || (p?.name || "?").split(" ").map((w) => w[0]).slice(0, 2).join("")}</div>`;
const Warmth = ({ w }) => w ? html`<span class="warmth ${w.label}"><i></i>${w.label}${w.months_since != null ? ` · ${w.months_since < 1 ? "this month" : `${Math.round(w.months_since)} mo`}` : ""}</span>` : null;
const Strength = ({ s }) => html`<span class="strength" title=${`link strength ${Math.round(s * 100)}`}>${[1, 2, 3].map((k) => html`<i class=${k <= strengthBars(s) ? "on" : ""}></i>`)}</span>`;

function ResultsGrid({ r }) {
  if (!r) return null;
  return html`<div>
    <div class="rgrid">
      <div class="h">${r.period} · ₹ cr</div><div class="h r">Value</div><div class="h r">QoQ</div><div class="h r">YoY</div>
      ${r.metrics.map((m) => html`
        <div>${m.label}</div>
        <div class="r num">${num(m.value)}</div>
        <div class="r delta ${tone(m.qoq)}">${sgn(m.qoq)}</div>
        <div class="r delta ${tone(m.yoy)}">${sgn(m.yoy)}</div>`)}
    </div>
    ${r.margin && html`<div class="small ink2">${r.margin.label} <b class="num">${r.margin.value?.toFixed(1)}%</b>
      <span class="delta ${tone(r.margin.yoy_bps)}"> ${sgn(r.margin.yoy_bps, 0, " bps")} YoY</span></div>`}
    <p class="take">${r.take}</p>
  </div>`;
}

function Timeline({ roles, highlightOrgs = [] }) {
  if (!roles?.length) return null;
  const toY = (s) => (s ? +s.slice(0, 4) + ((+s.slice(5, 7) || 1) - 1) / 12 : new Date().getFullYear() + new Date().getMonth() / 12);
  const lo = Math.min(...roles.map((r) => toY(r.from))), hi = Math.max(...roles.map((r) => toY(r.to)));
  const span = hi - lo || 1;
  return html`<div class="tl">
    ${roles.map((r) => {
      const a = ((toY(r.from) - lo) / span) * 100, b = ((toY(r.to) - lo) / span) * 100;
      const cls = !r.to ? "cur" : highlightOrgs.includes(r.org) ? "hl" : "";
      return html`<div class="tl-row" title=${`${r.title}, ${r.org_name}`}>
        <div class="lbl">${r.org_name}</div>
        <div class="tl-bar"><i class=${cls} style=${`left:${a}%;width:${Math.max(b - a, 2)}%`}></i></div>
      </div>`;
    })}
    <div class="tl-axis"><div></div><div><span>${Math.floor(lo)}</span><span>${hi >= new Date().getFullYear() ? "now" : Math.floor(hi)}</span></div></div>
  </div>`;
}

function PathView({ path, target, onPerson }) {
  if (!path) return null;
  const s = path.link ?? path.score;
  return html`<div>
    <div class="path">
      <div class="node"><div class="avatar you">You</div><span>You</span></div>
      ${path.via ? html`
        <div class="edge s${strengthBars(path.warmth?.score || 0)}"></div>
        <div class="node" onClick=${() => onPerson(path.via.id)} style="cursor:pointer"><${Avatar} p=${path.via} /><span>${path.via.name.split(" ")[0]}</span></div>
        <div class="edge s${strengthBars(s)}"></div>` : html`<div class="edge s1"></div>`}
      <div class="node"><${Avatar} p=${target} cls="target" /><span>${target.name.split(" ")[0]}</span></div>
    </div>
    ${path.via ? html`<p class="because"><b>${path.via.name}</b> <${Warmth} w=${path.warmth} /> · ${path.why[0]}</p>`
               : html`<p class="because">${path.why[0]}</p>`}
  </div>`;
}

function PeopleList({ people, onPerson, compact }) {
  if (!people?.length) return html`<p class="small muted">No one in your network is close to this yet.</p>`;
  return html`<div class="stack">${people.map((x) => html`<${RecRow} key=${x.person.id} x=${x} onPerson=${onPerson} compact=${compact} />`)}</div>`;
}

// ---------------------------------------------------------------------------
// Brief (Push)
// ---------------------------------------------------------------------------
const LEVELS = [["critical", "Critical"], ["material", "Material"], ["all", "Everything"]];
const WINDOWS = ["24h", "3d", "7d", "30d"];

function Dial({ level, setLevel }) {
  const idx = LEVELS.findIndex((l) => l[0] === level);
  return html`<div class="dial" role="radiogroup" aria-label="Signal level">
    <div class="dial-track">
      <div class="dial-fill" style=${`width:${idx * 50}px`}></div>
      ${LEVELS.map((l, i) => html`<button class="dial-stop ${i === idx ? "on" : i < idx ? "lit" : ""}" style=${`left:${i * 50}px`}
        role="radio" aria-checked=${i === idx} title=${l[1]} onClick=${() => setLevel(l[0])}><i></i></button>`)}
    </div>
    <span class="dial-label">${LEVELS[idx][1]}</span>
  </div>`;
}

function BriefView({ openPerson, openRules, rulesVersion }) {
  const [win, setWin] = useState(() => { try { return localStorage.getItem("finn.win") || "24h"; } catch { return "24h"; } });
  const [level, setLevel] = useState(() => { try { return localStorage.getItem("finn.level") || "material"; } catch { return "material"; } });
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const [offline, setOffline] = useState(false);
  const [toast, setToast] = useState(null);
  const online = useOnline();
  const inst = useInstall();
  const load = useCallback(async () => {
    setErr(null);
    try {
      const r = await fetch(`/api/brief?window=${win}&level=${level}`);
      if (!r.ok) throw new Error(r.status === 503 ? "You're offline and there's no saved brief yet." : `${r.status}`);
      setOffline(r.headers.get("X-FINN-Offline") === "1");
      setData(await r.json());
    } catch (e) { setErr(e.message); }
  }, [win, level]);
  useEffect(() => { try { localStorage.setItem("finn.win", win); localStorage.setItem("finn.level", level); } catch {} load(); }, [win, level, rulesVersion]);
  const { pull, busy: pulling } = usePullToRefresh(load);
  const refresh = async () => { setBusy(true); try { await api("/api/refresh", { method: "POST" }); await load(); } finally { setBusy(false); } };

  // Swipe a card away = "handled". Saved server-side (every device agrees); undo for 5 seconds.
  const handle = useCallback(async (item) => {
    setData((d) => ({ ...d, items: d.items.filter((x) => x.id !== item.id), alerts: d.alerts.filter((x) => x.id !== item.id),
                      counts: { ...d.counts, done: (d.counts.done || 0) + 1 } }));
    setToast({ item, t: Date.now() });
    try { await api("/api/brief/dismiss", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: item.id }) }); } catch {}
    load();  // headline and counts come from the server ("2 things left")
  }, [load]);
  const undo = async () => {
    const it = toast?.item;
    setToast(null);
    if (!it) return;
    await api("/api/brief/dismiss", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: it.id, undo: true }) });
    load();
  };
  useEffect(() => { if (!toast) return; const k = setTimeout(() => setToast(null), 5000); return () => clearTimeout(k); }, [toast]);

  const today = new Date().toLocaleDateString("en-IN", { weekday: "short", day: "numeric", month: "short" });
  const left = data ? data.items.length + data.alerts.length : 0;
  const asOf = data?.as_of ? new Date(data.as_of).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" }) : "";
  return html`<div>
    <div class="ptr" style=${`height:${pull}px`}><span class=${pulling ? "spin-icon" : ""} style=${`opacity:${Math.min(1, pull / 64)};transform:rotate(${pull * 3}deg)`}>${Icon.refresh}</span></div>
    <div class="topbar">
      <div class="wordmark">FINN <small>${today}</small></div>
      <div style="display:flex;gap:2px">
        <button class="iconbtn" aria-label="Refresh sources" title="Refresh sources" onClick=${refresh} style=${busy ? "animation:spin 1s linear infinite" : ""}>${Icon.refresh}</button>
        <button class="iconbtn" aria-label="Alert rules and notifications" title="Alert rules" onClick=${openRules}>${Icon.sliders}</button>
      </div>
    </div>
    ${(!online || offline) && html`<div class="offline">Offline · showing your last brief${asOf ? ` from ${asOf}` : ""}</div>`}
    ${inst.canPrompt && html`<button class="installbar" onClick=${inst.install}><img src="/static/icons/icon-192.png" alt="" /><span><b>Install FINN</b><br /><span class="tiny">One tap from your home screen, alerts on your lock screen</span></span></button>`}
    ${inst.iosHint && html`<div class="installbar ios"><img src="/static/icons/icon-192.png" alt="" /><span><b>Add FINN to your Home Screen</b><br /><span class="tiny">Tap Share, then “Add to Home Screen”, to get alerts on iPhone</span></span><button class="linkish tiny" onClick=${(e) => { inst.hideHint(); e.currentTarget.parentElement.remove(); }}>Not now</button></div>`}
    ${err && html`<div class="err">Couldn't load the brief: ${err}</div>`}
    ${!data && !err && html`<div class="section"><div class="skeleton" style="height:70px;background:none;border:0;box-shadow:none"></div><div class="skeleton"></div><div class="skeleton"></div></div>`}
    ${data && html`<div>
      <div class="hero">
        <h1>${data.headline.split(". ")[0]}.</h1>
        <p>${data.headline.split(". ").slice(1).join(". ")}</p>
      </div>
      <div class="controls">
        <div class="seg" role="tablist" aria-label="Time window">${WINDOWS.map((w) => html`<button role="tab" aria-selected=${w === win} class=${w === win ? "on" : ""} onClick=${() => setWin(w)}>${w}</button>`)}</div>
        <${Dial} level=${level} setLevel=${setLevel} />
      </div>

      ${data.alerts.length > 0 && html`<div class="section">
        <div class="section-h"><h2>Moved</h2><span class="tiny muted">End of day ${data.eod_date ? new Date(data.eod_date).toLocaleDateString("en-IN", { weekday: "short", day: "numeric", month: "short" }) : ""}</span></div>
        ${data.alerts.map((a) => html`<${Swipeable} key=${a.id} onSwipe=${() => handle(a)}><div class="alert-row" onClick=${() => go(`/c/${a.ticker}`)}>
          <${Spark} data=${a.metrics.spark} />
          <div><div class="t">${a.company} <span class="tick">${a.ticker}</span></div>
            <div class="s">${a.unexplained ? html`<span class="badge warn">No filing explains this</span>` : html`Likely: ${a.explained_by}`}</div></div>
          <div class="chg"><div class=${tone(a.metrics.change_pct)}>${sgn(a.metrics.change_pct, 1)}</div>
            <div class="tiny muted">vol ${a.metrics.volume_x?.toFixed(1)}×<span class="volbar"><i style=${`width:${Math.min(100, (a.metrics.volume_x / 4) * 100)}%`}></i></span></div></div>
        </div><//>`)}
      </div>`}

      <div class="section">
        ${data.items.length > 0 && html`<div class="section-h"><h2>What matters</h2><span class="tiny muted">swipe when handled</span></div>`}
        ${data.items.map((i) => html`<${Swipeable} key=${i.id} onSwipe=${() => handle(i)}><${ItemCard} i=${i} openPerson=${openPerson} /><//>`)}
        ${data.counts.hidden_material > 0 && html`<button class="fold" style="width:100%;text-align:left" onClick=${() => setLevel("material")}>
          ${data.counts.hidden_material} material item${data.counts.hidden_material > 1 ? "s" : ""} below your dial. <span class="linkish">Show material</span></button>`}
        <${RoutineFold} r=${data.routine} count=${data.counts.routine} level=${level} setLevel=${setLevel} />
      </div>

      ${data.nudges?.length > 0 && html`<div class="section">
        <div class="section-h"><h2>Keep in touch</h2><button class="linkish small" onClick=${() => go("/network")}>All</button></div>
        <div class="card" style="padding:2px 14px">${data.nudges.map((n) => html`<div class="nudge" style="cursor:pointer" onClick=${() => openPerson(n.person.id)}>
          <div class="person"><${Avatar} p=${n.person} /><div style="flex:1;min-width:0"><div class="pname">${n.person.name} <${Warmth} w=${n.warmth} /></div>
            <div class="small ink2">${n.reason}</div></div></div></div>`)}</div>
      </div>`}

      ${!data.nudges?.length && data.reconnect.length > 0 && html`<div class="section">
        <div class="section-h"><h2>Natural reasons to reconnect</h2></div>
        ${data.reconnect.map((r) => html`<div class="card" style="padding:12px">
          <div class="person" style="cursor:pointer" onClick=${() => openPerson(r.person.id, r)}>
            <${Avatar} p=${r.person} />
            <div style="flex:1"><div class="pname">${r.person.name} <${Warmth} w=${r.warmth} /></div>
              <div class="phead">${r.org} (${r.relation}) filed <b>${r.event.title}</b></div></div>
          </div>
        </div>`)}
      </div>`}

      <div class="caughtup"><span class="serif">${left ? "That's everything that matters." : "You're caught up."}</span>
        ${data.counts.filings} filings read for you${data.counts.done ? ` · ${data.counts.done} handled` : ""}${data.last_refresh?.at ? ` · sources refreshed ${ago(data.last_refresh.at)}` : ""}</div>
    </div>`}
    ${toast && html`<div class="toast" role="status"><span>Handled: ${toast.item.company}</span><button onClick=${undo}>Undo</button></div>`}
  </div>`;
}

function Swipeable({ onSwipe, children }) {
  const ref = useSwipe(onSwipe);
  return html`<div class="swipe-wrap"><div class="swipe-under" aria-hidden="true"><span>Handled ✓</span><span>Handled ✓</span></div><div ref=${ref} class="swipe">${children}</div></div>`;
}

function RoutineFold({ r, count, level, setLevel }) {
  const [open, setOpen] = useState(false);
  if (!count) return null;
  return html`<div class="fold">
    <div style="display:flex;justify-content:space-between;align-items:center;gap:8px">
      <span><b>${count}</b> routine filing${count > 1 ? "s" : ""} folded away</span>
      <button class="linkish small" onClick=${() => (level === "all" ? setOpen(!open) : (setLevel("all"), setOpen(true)))}>${open && level === "all" ? "Hide" : "Show"}</button>
    </div>
    <div class="chips">${r.groups.map(([g, n]) => html`<span class="chip">${g}<b>${n}</b></span>`)}</div>
    ${open && level === "all" && html`<div style="margin-top:10px">${r.items.map((x) => html`<div class="rline">
      <span class="tick link" onClick=${() => go(`/c/${x.ticker}`)}>${x.ticker}</span>
      <div><div>${x.title}</div>${x.one_liner && html`<div class="small muted">${x.one_liner}</div>`}</div>
    </div>`)}</div>`}
  </div>`;
}

function ItemCard({ i, openPerson }) {
  const [why, setWhy] = useState(false);
  const [graph, setGraph] = useState(false);
  return html`<article class="card ${i.tier}">
    <div class="meta">
      <span class="dot ${i.tier}"></span>
      <span class="tick link" onClick=${() => go(`/c/${i.ticker}`)}>${i.ticker}</span>
      <span>${i.category_label}</span><span>·</span><span>${ago(i.ts)}</span>
      ${i.alert && html`<span class="badge alert">Alert</span>`}
      ${i.simulated && html`<span class="badge sim" title="Injected demo scenario, not a real filing">Simulated</span>`}
    </div>
    <h3>${i.company}: ${i.title}</h3>
    ${i.one_liner && !i.results && html`<p class="one">${i.one_liner}</p>`}

    ${i.results && html`<${ResultsGrid} r=${i.results} />`}

    ${i.person && html`<div class="callout">
      <span class="upper">Who's coming in</span>
      <div class="person" style="cursor:pointer" onClick=${() => openPerson(i.person.id)}>
        <${Avatar} p=${i.person} cls="lg target" />
        <div><div class="pname" style="font-size:15px">${i.person.name}</div><div class="phead">${i.person.headline}</div>
          ${i.person.credentials?.length > 0 && html`<div class="phead">${i.person.credentials.join(" · ")}</div>`}</div>
      </div>
      <${Timeline} roles=${i.person.roles} highlightOrgs=${(i.paths || []).flatMap((p) => p.orgs || [])} />
      <div class="tiny muted" style="margin-top:2px"><span style="display:inline-block;width:10px;height:6px;border-radius:2px;background:var(--accent);vertical-align:middle"></span> where your network overlapped with him</div>
      ${i.paths?.length > 0 && html`<div style="margin-top:12px"><span class="upper">Your way in</span>
        <${PathView} path=${i.paths.find((p) => p.via) || i.paths[0]} target=${i.person} onPerson=${openPerson} />
        ${i.paths.filter((p) => p.via).slice(1).map((p) => html`<div class="small ink2" style="margin-top:4px">Also: <b>${p.via.name}</b>: ${p.why[0]}</div>`)}
        ${i.paths.filter((p) => !p.via).map((p) => html`<div class="banner info" style="margin-top:10px"><b>You've crossed paths before.</b> ${p.why[0]}</div>`)}
      </div>`}
    </div>`}

    ${i.best_call && html`<div class="callout">
      <span class="upper">Best person to call</span>
      <div class="person" style="cursor:pointer" onClick=${() => openPerson(i.best_call.person.id, { event: i })}>
        <${Avatar} p=${i.best_call.person} />
        <div style="flex:1"><div style="display:flex;justify-content:space-between;gap:6px"><span class="pname">${i.best_call.person.name}</span><${Warmth} w=${i.best_call.warmth} /></div>
          <div class="phead">${i.best_call.person.headline}</div></div>
      </div>
      <p class="because">${i.best_call.because}</p>
      <button class="linkish tiny tap" onClick=${() => setGraph(!graph)}>${graph ? "Hide" : "Why this person?"} · ${Math.round(i.best_call.score * 100)}% match</button>
      ${graph && html`<${WhyPerson} rec=${i.best_call} />`}
    </div>`}

    ${!i.best_call && !i.person && i.people?.length > 0 && html`<div class="callout"><span class="upper">Close to this</span>
      <${PeopleList} people=${i.people} onPerson=${openPerson} compact /></div>`}

    <${WebContext} i=${i} openPerson=${openPerson} />

    ${why && html`<div class="why"><ul>${i.why_ranked.map((w) => html`<li>${w}</li>`)}</ul></div>`}
    <div class="card-foot">
      <button class="linkish" onClick=${() => setWhy(!why)}>${why ? "Hide" : "Why"} ranked ${Math.round(i.score)}</button>
      <button class="askbtn" onClick=${() => go(`/chat?t=${i.ticker}&title=${encodeURIComponent(`${i.company}: ${i.title}`)}`)}>Ask FINN</button>
      <span>${i.related?.length ? `+${i.related.length} related · ` : ""}${i.url ? html`<a href=${i.url} target="_blank" rel="noopener">Filing ↗</a>` : html`<span>no source (simulated)</span>`}</span>
    </div>
  </article>`;
}

// ---------------------------------------------------------------------------
// Dossier (Pull)
// ---------------------------------------------------------------------------
const VERDICT = { delivered: "Delivered", missed: "Missed", partly: "Partly", on_track: "On track", behind: "Behind", pending: "Pending", unverifiable: "Can't check", mention: "Unverified" };

function DossierView({ ticker, openPerson }) {
  const [d, setD] = useState(null);
  const [err, setErr] = useState(null);
  useEffect(() => { setD(null); setErr(null); window.scrollTo(0, 0); api(`/api/company/${ticker}`).then(setD).catch((e) => setErr(e.message)); }, [ticker]);
  const back = html`<div class="topbar"><button class="iconbtn" onClick=${() => history.length > 1 ? history.back() : go("/")}>${Icon.back}</button><span class="tick">${ticker}</span><span style="width:34px"></span></div>`;
  if (err) return html`<div>${back}<div class="err">${err}</div></div>`;
  if (!d) return html`<div>${back}<div class="loading">Reading ${ticker}'s filings and transcripts…<div style="margin-top:14px" class="skeleton"></div><div class="skeleton"></div></div></div>`;
  const s = d.snapshot, L = d.ledger, sc = L.score || {};
  return html`<div>
    ${back}
    <div class="dhead">
      <div class="meta">${[...new Set((d.sector_path || []).slice(1, 4))].slice(0, 2).join(" › ")}${d.in_portfolio && html` · <span class="badge alert">${d.weight}% of portfolio</span>`}</div>
      <h1>${d.name}</h1>
      ${d.price && html`<div class="price-line"><span class="p">₹${num(d.price.close, 1)}</span>
        <span class="delta ${tone(d.price.change_pct)}">${sgn(d.price.change_pct, 1)}</span><${Spark} data=${d.price.spark} w=${90} h=${26} />
        <span class="tiny muted">vol ${d.price.volume_x?.toFixed(1)}×</span></div>`}
      <button class="pbtn" style="margin-top:12px" onClick=${() => go(`/chat?t=${ticker}`)}>Ask FINN about ${ticker}</button>
    </div>
    <nav class="stickynav">
      ${[["promises", "Promises"], ["results", "Latest quarter"], ["numbers", "Numbers"], ["cash", "Cash & debt"], ["case", "Bull / bear"], ["people", "People"], ["filings", "Filings"]]
        .map(([id, l]) => html`<a href=${"#"} onClick=${(e) => { e.preventDefault(); document.getElementById(id)?.scrollIntoView({ behavior: "smooth" }); }}>${l}</a>`)}
    </nav>

    <section class="dsec" id="promises">
      <h2>Promises vs delivery</h2>
      <p class="sub">What management guided on the last ${L.calls?.length || 0} earnings calls, checked against what they then reported.</p>
      ${L.method === "heuristic" && html`<div class="banner">Pattern-matched from transcripts: these are guidance-shaped quotes, not verified promises. Add a model key to extract and verify them against reported numbers.</div>`}
      ${L.method !== "heuristic" && html`<div class="scoreboard">
        <div><b class="pos">${sc.delivered || 0}</b><span>Delivered</span></div>
        <div><b class="neg">${sc.missed || 0}</b><span>Missed</span></div>
        <div><b>${sc.on_track || 0}</b><span>On track</span></div>
        <div><b class=${sc.behind ? "neg" : ""}>${sc.behind || 0}</b><span>Behind</span></div>
      </div>`}
      ${L.observations?.length > 0 && html`<ul class="obs">${L.observations.map((o) => html`<li>${o}</li>`)}</ul>`}
      ${!L.items?.length && html`<p class="small muted">${L.calls?.length ? "No explicit numeric guidance found in these calls." : "No concall transcripts are published for this company."}</p>`}
      <div style="margin-top:6px">${L.items.map((it) => html`<${LedgerItem} it=${it} />`)}</div>
    </section>

    ${d.latest_results && html`<section class="dsec" id="results">
      <h2>Latest quarter</h2><p class="sub">${d.basis} · ${d.latest_results.filing_pdf ? html`<a class="linkish" href=${d.latest_results.filing_pdf} target="_blank" rel="noopener">results filing ↗</a>` : ""}</p>
      <div class="card"><${ResultsGrid} r=${d.latest_results} /></div>
    </section>`}

    <section class="dsec" id="numbers">
      <h2>The numbers</h2><p class="sub">${d.business?.what_it_does || (d.sector_path || []).join(" › ")}</p>
      ${d.business?.segments?.length > 0 && html`<div class="chips" style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px">${d.business.segments.map((g) => html`<span class="chip">${g.name}${g.share_pct != null ? html`<b>${g.share_pct}%</b>` : ""}</span>`)}</div>`}
      <div class="kv">
        <div><span>Mkt cap</span><b>${num(s.market_cap_cr)}</b></div>
        <div><span>P/E</span><b>${num(s.pe, 1)}</b></div>
        <div><span>${s.lender ? "ROE" : "ROCE"}</span><b>${num(s.lender ? s.roe : s.roce, 1)}%</b></div>
        <div><span>Revenue TTM</span><b>${num(s.revenue_ttm)}</b></div>
        <div><span>PAT TTM</span><b>${num(s.pat_ttm)}</b></div>
        <div><span>${s.lender ? "Op. margin" : "EBITDA m."}</span><b>${num(s.margin_ttm, 1)}%</b></div>
      </div>
      <div class="small muted">₹ crore. Revenue CAGR 3y <b class="num">${sgn(d.trajectory.cagr.revenue_3y, 0)}</b> · 5y <b class="num">${sgn(d.trajectory.cagr.revenue_5y, 0)}</b> · PAT 3y <b class="num">${sgn(d.trajectory.cagr.pat_3y, 0)}</b></div>
      <${BarLine} labels=${d.trajectory.years} bars=${d.trajectory.revenue} line=${d.trajectory.margin} barLabel="Revenue (annual)" lineLabel=${s.lender ? "Op. margin %" : "EBITDA margin %"} />
      <ul class="flags">${d.trajectory.flags.map((f) => html`<li class=${f.tone}>${f.text}</li>`)}</ul>
      <${BarLine} labels=${d.trajectory.quarters.periods} bars=${d.trajectory.quarters.revenue} line=${d.trajectory.quarters.margin} barLabel="Revenue (quarterly)" lineLabel="Margin %" h=${100} />
    </section>

    <section class="dsec" id="cash">
      <h2>Cash & balance sheet</h2><p class="sub">Do profits turn into cash, and what funds the growth?</p>
      <ul class="flags">${[...d.cash.flags, ...d.balance_sheet.flags].map((f) => html`<li class=${f.tone}>${f.text}</li>`)}</ul>
      <table class="minitable">
        <thead><tr><th></th>${d.cash.periods.slice(-5).map((p) => html`<th>${shortPeriod(p)}</th>`)}</tr></thead>
        <tbody>
          <tr><td>Cash from ops</td>${d.cash.cfo.slice(-5).map((v) => html`<td>${num(v)}</td>`)}</tr>
          <tr><td>PAT</td>${d.cash.pat.slice(-5).map((v) => html`<td>${num(v)}</td>`)}</tr>
          ${!d.cash.lender && html`<tr><td>CFO / PAT</td>${d.cash.conversion.slice(-5).map((v) => html`<td class=${v == null ? "" : v >= 90 ? "pos" : v < 70 ? "neg" : ""}>${v == null ? "–" : v + "%"}</td>`)}</tr>`}
          ${d.cash.fcf?.length > 0 && html`<tr><td>Free cash flow</td>${d.cash.fcf.slice(-5).map((v) => html`<td class=${tone(v)}>${num(v)}</td>`)}</tr>`}
        </tbody>
      </table>
      <table class="minitable" style="margin-top:12px">
        <thead><tr><th></th>${d.balance_sheet.periods.slice(-5).map((p) => html`<th>${shortPeriod(p)}</th>`)}</tr></thead>
        <tbody>
          <tr><td>Borrowings</td>${d.balance_sheet.borrowings.slice(-5).map((v) => html`<td>${num(v)}</td>`)}</tr>
          ${!d.balance_sheet.lender && html`<tr><td>Debt / equity</td>${d.balance_sheet.de.slice(-5).map((v) => html`<td>${v == null ? "–" : v.toFixed(2)}</td>`)}</tr>`}
        </tbody>
      </table>
      ${!d.balance_sheet.lender && d.balance_sheet.wc_days["Cash Conversion Cycle"]?.length > 0 && html`<table class="minitable" style="margin-top:12px">
        <thead><tr><th>Days</th>${d.balance_sheet.eff_periods.slice(-5).map((p) => html`<th>${shortPeriod(p)}</th>`)}</tr></thead>
        <tbody>${["Debtor Days", "Inventory Days", "Days Payable", "Cash Conversion Cycle"].map((k) => html`<tr><td>${k.replace(" Days", "")}</td>${(d.balance_sheet.wc_days[k] || []).slice(-5).map((v) => html`<td>${num(v)}</td>`)}</tr>`)}</tbody>
      </table>`}
    </section>

    <section class="dsec" id="case">
      <h2>Bull vs bear</h2><p class="sub">${d.bull_bear.source === "model" ? "Argued by the deep model from the facts above only." : "Assembled from the computed signals above."}</p>
      <div class="bb">
        <div class="col bull"><h3>Bull</h3><ol>${d.bull_bear.bull.map((b) => html`<li>${b.point}<small>${b.evidence}</small></li>`)}</ol>${!d.bull_bear.bull.length && html`<p class="small muted">Nothing stands out.</p>`}</div>
        <div class="col bear"><h3>Bear</h3><ol>${d.bull_bear.bear.map((b) => html`<li>${b.point}<small>${b.evidence}</small></li>`)}</ol>${!d.bull_bear.bear.length && html`<p class="small muted">Nothing stands out.</p>`}</div>
      </div>
    </section>

    <section class="dsec" id="people">
      ${d.cause && html`<h2>Who can explain the latest update</h2>
        <p class="sub">${d.cause.filing.category_label} · ${ago(d.cause.filing.ts)}: ${d.cause.filing.title}. Ranked for this event and subject, not the company in general.</p>
        <div class="card"><${PeopleList} people=${d.cause.people} onPerson=${openPerson} />
          <div class="tiny muted" style="margin-top:8px">${d.cause.coverage.level === "strong" ? "Your network covers this: " : "Coverage is thin: "}${d.cause.coverage.reason}.</div>
          <${WebLeads} query=${d.cause.web_query} auto=${d.cause.coverage.search_web} /></div>`}
      <h2 style=${d.cause ? "margin-top:22px" : ""}>Who can help</h2><p class="sub">People in your network closest to ${d.name}${d.themes?.length ? `, its competitors, or ${d.themes.join(", ")}` : ""}.</p>
      <div class="card"><${PeopleList} people=${d.people} onPerson=${openPerson} />
        ${!d.cause && !d.people?.length && html`<${WebLeads} query=${{ company: d.name, theme: d.themes?.[0], text: d.name }} auto=${true} />`}</div>
    </section>

    <section class="dsec" id="filings">
      <h2>Recent filings</h2>
      <div class="card" style="padding:4px 14px">${d.filings.map((f) => html`<div class="rline">
        <span class="dot ${f.tier}" style="margin-top:7px"></span>
        <div><a href=${f.url} target="_blank" rel="noopener" style="text-decoration:none">${f.title}</a>
          <div class="small muted">${f.category_label} · ${ago(f.ts)}${f.summary ? ` · ${f.summary.slice(0, 120)}` : ""}</div></div>
      </div>`)}</div>
      <p class="tiny muted" style="margin-top:14px">Sources: ${L.calls?.length || 0} concall transcripts (${(L.calls || []).join(", ")}), exchange filings, reported financials (${d.basis}). Generated ${ago(d.generated_at)}.
        ${d.documents.annual_reports?.[0] && html` <a class="linkish" href=${d.documents.annual_reports[0].url} target="_blank" rel="noopener">Annual report ${d.documents.annual_reports[0].year} ↗</a>`}</p>
    </section>
  </div>`;
}

function LedgerItem({ it }) {
  const [open, setOpen] = useState(false);
  const unit = it.kind === "absolute_cr" ? " cr" : "%";
  return html`<div class="ledger-item" onClick=${() => setOpen(!open)} style="cursor:pointer">
    <div class="li-top">
      <div>
        <div class="li-metric">${it.metric} <span>· ${it.horizon} · said ${it.promised_in}</span></div>
        <div class="li-vs">${it.promised}${it.actual != null && html`<span class="arrow">→</span><b class=${["missed", "behind"].includes(it.verdict) ? "neg" : "pos"}>${num(it.actual, 1)}${unit}</b> <span class="muted">${it.window || ""}</span>`}</div>
        ${it.revised && html`<div class="revised">Revised ${it.revised.direction} to ${it.revised.to} in ${it.revised.in}</div>`}
        ${it.reiterated > 0 && !it.revised && html`<div class="tiny muted">Reiterated on ${it.reiterated} later call${it.reiterated > 1 ? "s" : ""}</div>`}
      </div>
      <span class="verdict v-${it.verdict}">${VERDICT[it.verdict] || it.verdict}</span>
    </div>
    ${open && html`<blockquote class="quote">“${it.quote}”<cite>${it.speaker ? `${it.speaker}, ` : ""}${it.promised_in} call, p.${it.page} · <a href=${it.url} target="_blank" rel="noopener" onClick=${(e) => e.stopPropagation()}>transcript ↗</a>${it.note ? ` · ${it.note}` : ""}</cite></blockquote>`}
  </div>`;
}

// ---------------------------------------------------------------------------
// Portfolio + Network tabs
// ---------------------------------------------------------------------------
function PortfolioView() {
  const [d, setD] = useState(null);
  const [sort, setSort] = useState("move");
  useEffect(() => { api("/api/portfolio").then(setD); }, []);
  const rows = useMemo(() => {
    if (!d) return [];
    const r = [...d.holdings];
    if (sort === "move") r.sort((a, b) => Math.abs(b.metrics.change_pct || 0) - Math.abs(a.metrics.change_pct || 0));
    if (sort === "volume") r.sort((a, b) => (b.metrics.volume_x || 0) - (a.metrics.volume_x || 0));
    if (sort === "weight") r.sort((a, b) => b.weight - a.weight);
    return r;
  }, [d, sort]);
  return html`<div>
    <div class="topbar"><div class="wordmark">Portfolio <small>${d ? `${d.holdings.length} holdings` : ""}</small></div></div>
    <div class="controls"><div class="seg">${[["move", "Biggest move"], ["volume", "Volume"], ["weight", "Weight"]].map(([k, l]) => html`<button class=${sort === k ? "on" : ""} onClick=${() => setSort(k)}>${l}</button>`)}</div></div>
    <div class="section">
      ${!d && html`<div class="skeleton"></div>`}
      <div class="card" style="padding:2px 14px">${rows.map((h) => html`<div class="list-row" onClick=${() => go(`/c/${h.ticker}`)}>
        <div style="min-width:0"><div class="pname">${h.name}</div><div class="phead"><span class="tick">${h.ticker}</span> ${h.weight}% · ${h.theme}</div></div>
        <div style="display:flex;align-items:center;gap:10px">
          <${Spark} data=${h.metrics.spark} />
          <div style="text-align:right;min-width:58px"><div class="delta ${tone(h.metrics.change_pct)}">${sgn(h.metrics.change_pct, 1)}</div>
            <div class="tiny ${h.metrics.volume_x >= 2 ? "" : "muted"}" style=${h.metrics.volume_x >= 2 ? "color:var(--accent);font-weight:600" : ""}>${h.metrics.volume_x?.toFixed(1)}× vol</div></div>
        </div>
      </div>`)}</div>
      ${d && html`<div class="section-h"><h2>Research list</h2></div>
        <div class="card" style="padding:2px 14px">${d.research.map((h) => html`<div class="list-row" onClick=${() => go(`/c/${h.ticker}`)}>
          <div><div class="pname">${h.name}</div><div class="phead"><span class="tick">${h.ticker}</span> ${h.theme}</div></div><span class="muted">${Icon.arrow}</span></div>`)}</div>
        <p class="tiny muted" style="text-align:center;margin-top:12px">Any NSE ticker works: type it in the ask bar.</p>`}
    </div>
  </div>`;
}

// ---------------------------------------------------------------------------
// Flow 1: people from the public web when the network has no one
// ---------------------------------------------------------------------------
const host = (u) => { try { return new URL(u).hostname.replace(/^www\./, ""); } catch { return "source"; } };
const age = (y) => (y == null ? "" : y < 0.15 ? "this month" : y < 1 ? `${Math.round(y * 12)} mo old` : `${y.toFixed(0)} yr old`);
const qs = (o) => Object.entries(o).filter(([, v]) => v != null && v !== "").map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("&");

function WebLeads({ query, auto }) {
  const [st, setSt] = useState(auto ? "loading" : "idle");
  const [r, setR] = useState(null);
  const [added, setAdded] = useState({});
  const run = () => { setSt("loading"); api(`/api/network/discover?${qs(query)}`).then((x) => { setR(x); setSt("done"); }).catch((e) => { setR({ error: e.message }); setSt("done"); }); };
  useEffect(() => { if (auto) run(); }, [JSON.stringify(query)]);
  const add = async (c) => {
    const res = await api("/api/network/contacts", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: c.name, title: c.title, org_name: c.org_name, org_id: c.org_id, lead: true, source_url: c.source.url }) });
    haptic(15); setAdded({ ...added, [c.name]: res.existed ? "Already in your network" : "Added. FINN will nudge you to reach out." });
  };
  if (st === "idle") return html`<button class="pbtn ghost" style="margin-top:10px" onClick=${run}>Look beyond your network</button>`;
  if (st === "loading") return html`<div class="callout"><span class="upper">Searching the public web</span><div class="small muted">Finding named people and checking each one against its source. About 10 seconds.</div></div>`;
  if (r?.error || r?.status === "unavailable") return html`<p class="tiny muted">${r.reason || r.error}</p>`;
  return html`<div class="callout">
    <span class="upper">From the public web · not in your network</span>
    ${!r.candidates.length && html`<div class="small muted">No one the sources name clearly enough to suggest.</div>`}
    ${r.candidates.map((c) => html`<div class="lead">
      <div class="person"><${Avatar} p=${{ name: c.name }} cls="target" /><div style="min-width:0"><div class="pname">${c.name}</div><div class="phead">${c.title} · ${c.org_name}</div></div></div>
      ${c.why && html`<div class="small ink2" style="margin-top:6px">${c.why}</div>`}
      ${c.intro?.[0] && html`<div class="small" style="margin-top:6px">Way in: <b>${c.intro[0].name}</b> ${c.intro[0].relation}</div>`}
      <div class="tiny muted" style="margin-top:6px">${Math.round(c.fit * 100)}% fit · verified on <a href=${c.source.url} target="_blank" rel="noopener">${host(c.source.url)} ↗</a>${c.source.age_years != null ? ` · ${age(c.source.age_years)}` : ""}${c.verified.how === "result title" ? " · headline only" : ""}${c.current ? "" : " · may have moved on"}</div>
      ${c.source.quote && html`<div class="tiny ink2 srcq">${c.source.quote}</div>`}
      <div style="margin-top:8px">${added[c.name] ? html`<span class="tiny pos">${added[c.name]}</span>` : html`<button class="pbtn" onClick=${() => add(c)}>${Icon.plus} Add to network</button>`}</div>
    </div>`)}
    ${r.already_in_network?.length > 0 && html`<div class="tiny muted" style="margin-top:8px">Also named: ${r.already_in_network.map((x) => x.name).join(", ")}, already in your network.</div>`}
    <div class="tiny muted" style="margin-top:8px">Secondary source. Each name was checked against the page it came from; unverifiable names were dropped${r.stats?.unverified ? ` (${r.stats.unverified} this time)` : ""}.</div>
  </div>`;
}

// ---------------------------------------------------------------------------
// Flow 2: verified web context on a pushed filing
// ---------------------------------------------------------------------------
function WebContext({ i, openPerson }) {
  const [w, setW] = useState(i.web || null);
  const [busy, setBusy] = useState(false);
  if (!w) {
    if (i.simulated || i.tier === "routine") return null;
    const get = () => { setBusy(true); api(`/api/brief/${encodeURIComponent(i.id)}/web`).then(setW).catch(() => setW({ facts: [], people: [], status: "error" })).finally(() => setBusy(false)); };
    return html`<button class="linkish tiny tap" disabled=${busy} onClick=${get}>${busy ? "Reading the web…" : "Add context from the web"}</button>`;
  }
  if (!w.facts?.length && !w.people?.length) return html`<div class="tiny muted" style="margin-top:8px">The web adds nothing verifiable beyond the filing.</div>`;
  return html`<div class="callout webctx">
    <span class="upper">From the web · secondary to the filing</span>
    ${w.facts.map((f) => html`<div class="small" style="margin-top:4px">${f.text} <a class="tiny muted" href=${f.source.url} target="_blank" rel="noopener">${host(f.source.url)}${f.source.tier && f.source.tier !== "other" ? ` · ${f.source.tier}` : ""} ↗</a></div>`)}
    ${w.people.map((p) => html`<div style="margin-top:10px">
      <div class="small"><b>${p.name}</b> · ${p.event_role || p.title}</div>
      ${p.prior?.length > 0 && html`<div class="tiny muted">Before: ${p.prior.map((r) => `${r.title}, ${r.org_name}${r.from ? ` (${r.from}–${r.to || "now"})` : ""}`).join(" · ")}</div>`}
      ${p.routes?.length > 0 && html`<div class="tiny" style="margin-top:2px">Ask <b class="link" onClick=${() => openPerson(p.routes[0].id)}>${p.routes[0].name}</b>, who ${p.routes[0].relation === "works there now" ? `works at ${p.routes[0].via_org}` : p.routes[0].relation.replace("there", `at ${p.routes[0].via_org}`)}</div>`}
    </div>`)}
  </div>`;
}

// ---------------------------------------------------------------------------
// Flow 3: keep in touch, add people
// ---------------------------------------------------------------------------
function KeepInTouch({ openPerson, limit = 6 }) {
  const [d, setD] = useState(null);
  const [drafts, setDrafts] = useState({});
  const [done, setDone] = useState({});
  const load = () => api(`/api/network/nudges?limit=${limit}`).then(setD).catch(() => setD({ items: [] }));
  useEffect(() => { load(); }, []);
  if (!d) return html`<div class="skeleton"></div>`;
  if (!d.items.length) return html`<div class="card small muted">No one is overdue. Nice.</div>`;
  const touch = async (id) => { await api(`/api/network/contacts/${id}/touch`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }); haptic(15); setDone({ ...done, [id]: true }); };
  const draft = async (id) => { setDrafts({ ...drafts, [id]: "…" }); const r = await api(`/api/network/nudges/${id}/draft`, { method: "POST" }); setDrafts({ ...drafts, [id]: r.text }); };
  return html`<div class="card" style="padding:2px 14px">${d.items.map((n) => html`<div class="nudge">
    <div class="person" style="cursor:pointer" onClick=${() => openPerson(n.person.id)}><${Avatar} p=${n.person} cls=${n.lead ? "target" : ""} />
      <div style="flex:1;min-width:0"><div class="pname">${n.person.name} ${n.lead ? html`<span class="badge web">New lead</span>` : html`<${Warmth} w=${n.warmth} />`}</div>
        <div class="phead">${n.person.headline}</div></div>
      <div class="prio" title="Priority"><i style=${`width:${Math.round(n.scores.priority * 100)}%`}></i></div></div>
    <div class="small ink2" style="margin:6px 0 0">${done[n.person.id] ? "Logged. The clock restarts today." : n.reason}</div>
    ${!done[n.person.id] && html`<div style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap">
      <button class="pbtn" onClick=${() => touch(n.person.id)}>I spoke to them</button>
      <button class="pbtn ghost" onClick=${() => draft(n.person.id)}>Draft a note</button></div>`}
    ${drafts[n.person.id] && html`<div class="draft" contenteditable="true">${drafts[n.person.id]}</div>
      <div style="display:flex;gap:8px;align-items:center"><button class="linkish tiny" onClick=${() => navigator.clipboard?.writeText(drafts[n.person.id])}>Copy</button><span class="tiny muted">FINN never sends anything for you.</span></div>`}
  </div>`)}</div>`;
}

function AddContact({ onAdded }) {
  const [open, setOpen] = useState(false);
  const [f, setF] = useState({ name: "", title: "", org_name: "", context: "", last_contact: new Date().toISOString().slice(0, 10), tie_type: "met" });
  const [msg, setMsg] = useState("");
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });
  const save = async (e) => {
    e.preventDefault();
    try {
      const r = await api("/api/network/contacts", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(f) });
      haptic(15); setMsg(r.existed ? `${f.name} is already in your network.` : `Added ${f.name}.`); setOpen(false); onAdded?.();
      setF({ ...f, name: "", title: "", org_name: "", context: "" });
    } catch (err) { setMsg(err.message); }
  };
  if (!open) return html`<div style="display:flex;gap:10px;align-items:center"><button class="pbtn ghost" onClick=${() => { setOpen(true); setMsg(""); }}>${Icon.plus} Add someone</button>${msg && html`<span class="tiny ink2">${msg}</span>`}</div>`;
  return html`<form class="card addform" onSubmit=${save}>
    <label>Name<input required minlength="3" value=${f.name} onInput=${set("name")} autocomplete="off" /></label>
    <label>Role<input value=${f.title} onInput=${set("title")} placeholder="e.g. CFO" /></label>
    <label>Company<input value=${f.org_name} onInput=${set("org_name")} /></label>
    <label>How you know them<input value=${f.context} onInput=${set("context")} placeholder="e.g. met at the Kaynes analyst day" /></label>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
      <label>Relationship<select value=${f.tie_type} onChange=${set("tie_type")}>${["met", "ex-colleague", "classmate", "friend", "mentor", "expert"].map((t) => html`<option value=${t}>${t}</option>`)}</select></label>
      <label>Last spoke<input type="date" value=${f.last_contact} onInput=${set("last_contact")} /></label>
    </div>
    <div style="display:flex;gap:8px;margin-top:4px"><button class="pbtn" type="submit">Save</button><button class="pbtn ghost" type="button" onClick=${() => setOpen(false)}>Cancel</button></div>
  </form>`;
}

function ModelCard() {
  const [m, setM] = useState(null);
  useEffect(() => { api("/api/network/model").then(setM).catch(() => {}); }, []);
  if (!m) return null;
  const max = Math.max(...m.features.map((f) => Math.max(f.prior, f.learned)));
  return html`<div class="card">
    <span class="upper muted">How FINN ranks people</span>
    <p class="small ink2" style="margin:6px 0 10px">A two-stage recommender. It gathers candidates from four sources (career links, graph walks, domain similarity, topic match), ranks them with a model over the signals below, and keeps the top picks diverse. Your “Helpful / Not relevant” taps retrain the weights.</p>
    ${m.features.map((f) => html`<div class="crow"><span>${f.label}</span>
      <div class="bar" style="position:relative"><i style=${`width:${(f.learned / max) * 100}%`}></i><u style=${`left:${(f.prior / max) * 100}%`}></u></div>
      <b class="num">${f.learned.toFixed(1)}</b></div>`)}
    <div class="tiny muted" style="margin-top:6px">${m.n_feedback ? `Learned from ${m.n_feedback} of your ratings. Tick = starting weight.` : "No ratings yet: running on starting weights."}</div>
  </div>`;
}

function NetworkView({ openPerson, openAsk }) {
  const [d, setD] = useState(null);
  const [v, setV] = useState(0);
  useEffect(() => { api("/api/network").then(setD); }, [v]);
  const cold = d ? d.people.filter((p) => p.warmth.label === "dormant") : [];
  return html`<div>
    <div class="topbar"><div class="wordmark">Network <small>${d ? `${d.people.length} people` : ""}</small></div></div>
    <div class="hero"><h1 style="font-size:28px">What your network knows.</h1><p>Expertise inferred from careers, not tags. Tap a theme to ask.</p></div>
    <div class="section">
      <div class="section-h"><h2>Keep in touch</h2></div>
      <p class="tiny muted" style="margin:-4px 0 8px">Ranked by how far past your usual rhythm you are, how much they matter to your portfolio, and whether there's a reason to call now.</p>
      <${KeepInTouch} key=${v} openPerson=${openPerson} />
      <div style="margin:10px 0 18px"><${AddContact} onAdded=${() => setV(v + 1)} /></div>
      ${!d && html`<div class="skeleton"></div>`}
      ${d && html`<div class="card" style="padding:2px 14px">${d.themes.map((t) => html`<div class="theme-row">
        <h4 style="cursor:pointer" onClick=${() => openAsk(`who knows ${t.theme}?`)}>${t.theme}</h4>
        <div class="stack">${t.people.map((e) => html`<div style="display:grid;grid-template-columns:1fr 70px;gap:10px;align-items:center;cursor:pointer" onClick=${() => openPerson(e.person.id)}>
          <div class="small"><b>${e.person.name}</b> <span class="muted">· ${e.years ? `${e.years.toFixed(0)} yrs · ` : ""}${e.evidence?.[0] || e.why?.[0] || ""}</span></div>
          <div class="bar"><i style=${`width:${Math.round(e.score * 100)}%`}></i></div></div>`)}</div>
      </div>`)}</div>`}
      <div class="section-h"><h2>The ranking model</h2></div>
      <${ModelCard} />
      ${cold.length > 0 && html`<div class="section-h"><h2>Going cold</h2></div>
        <div class="card" style="padding:2px 14px">${cold.map((p) => html`<div class="list-row" onClick=${() => openPerson(p.id)}>
          <div class="person"><${Avatar} p=${p} /><div><div class="pname">${p.name}</div><div class="phead">${p.headline}</div></div></div><${Warmth} w=${p.warmth} /></div>`)}</div>`}
    </div>
  </div>`;
}

// ---------------------------------------------------------------------------
// Sheets: person, ask, rules
// ---------------------------------------------------------------------------
function Sheet({ onClose, children }) {
  useEffect(() => { const k = (e) => e.key === "Escape" && onClose(); addEventListener("keydown", k); return () => removeEventListener("keydown", k); }, []);
  const ref = useSheetDrag(onClose);  // drag the sheet down to dismiss, like a native bottom sheet
  return html`<div><div class="scrim" onClick=${onClose}></div><div class="sheet" role="dialog" aria-modal="true" ref=${ref}><div class="grab" aria-hidden="true"></div>${children}</div></div>`;
}

function PhoneAlerts() {
  const [st, setSt] = useState(null);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => { pushState().then(setSt).catch(() => setSt({ supported: false })); }, []);
  if (!st) return null;
  const toggle = async () => {
    setBusy(true); setMsg("");
    try {
      if (st.on) { await disablePush(); setMsg("Alerts off on this device."); }
      else { const r = await enablePush(); haptic(20); setMsg(`Alerts on. ${r.devices} device${r.devices > 1 ? "s" : ""} registered.`); }
      setSt(await pushState());
    } catch (e) { setMsg(e.message); } finally { setBusy(false); }
  };
  const test = async () => { const r = await api("/api/push/test", { method: "POST" }); setMsg(r.sent ? "Test alert sent. Check your notifications." : "No device received it yet."); };
  return html`<div class="rule-group phone">
    <div class="upper muted">Alerts on this phone</div>
    ${!st.supported ? html`<p class="small ink2" style="margin:6px 0">${st.reason === "ios-install" ? "On iPhone, add FINN to your Home Screen first (Share, then Add to Home Screen). Then turn alerts on here." : "This browser can't receive push alerts."}</p>`
      : html`<div class="switchrow"><span class="small">Critical filings and your price/volume triggers, on the lock screen</span>
          <button class=${`switch ${st.on ? "on" : ""}`} role="switch" aria-checked=${st.on} aria-label="Phone alerts" disabled=${busy} onClick=${toggle}><i></i></button></div>
        ${st.on && html`<button class="linkish small" onClick=${test}>Send a test alert</button>`}`}
    ${msg && html`<p class="tiny ink2" style="margin:6px 0 0">${msg}</p>`}
  </div>`;
}

function PersonSheet({ id, ctx, openPerson }) {
  const [p, setP] = useState(null);
  const [draft, setDraft] = useState(null);
  useEffect(() => { setP(null); setDraft(null); api(`/api/people/${id}`).then(setP); }, [id]);
  if (!p) return html`<div class="loading">Loading…</div>`;
  const reason = ctx?.event ? `${ctx.event.company}'s ${ctx.event.category_label.toLowerCase()} news` : ctx?.reason ? ctx.event?.title : null;
  const note = () => {
    const first = p.name.split(" ")[0];
    const topic = ctx?.event?.title ? `${(ctx.event.company || ctx.org || "").trim()}: ${ctx.event.title.toLowerCase()}` : ctx?.org ? `the news out of ${ctx.org}` : "what you're working on";
    setDraft(`Hi ${first}, it's been a while. Saw ${topic} and thought of you right away. Would love 20 minutes to get your read on it, and to catch up properly. Coffee next week?`);
  };
  return html`<div>
    <div class="person" style="align-items:center">
      <${Avatar} p=${p} cls="lg ${p.is_contact ? "" : "target"}" />
      <div><h2 style="margin:0">${p.name}</h2><div class="phead">${p.headline}${p.location ? ` · ${p.location}` : ""}</div></div>
    </div>
    ${p.simulated && html`<p class="small" style="margin:10px 0 0"><span class="badge sim">Simulated profile</span> <span class="muted">${p.source}</span></p>`}
    ${p.warmth && html`<div class="callout"><span class="upper">How you know ${p.name.split(" ")[0]}</span>
      <div class="small ink2">${p.warmth.context}</div>
      <div style="margin-top:6px"><${Warmth} w=${p.warmth} /> <span class="tiny muted">· last spoke ${p.warmth.last_contact}</span></div>
      ${p.overlap_with_you?.length > 0 && html`<div class="small ink2" style="margin-top:6px">${p.overlap_with_you[0]}</div>`}
    </div>`}
    ${p.paths?.length > 0 && html`<div class="callout"><span class="upper">Your way in</span>
      ${p.paths.map((x, k) => k === 0 ? html`<${PathView} path=${x} target=${p} onPerson=${openPerson} /><${EvidenceGraph} g=${x.graph} />`
        : html`<div class="small ink2" style="margin-top:6px">${x.via ? html`Or via <b style="cursor:pointer" onClick=${() => openPerson(x.via.id)}>${x.via.name}</b>: ` : ""}${x.why[0]}</div>`)}
    </div>`}
    <div class="section-h"><h2>Career</h2></div>
    <${Timeline} roles=${p.roles} />
    <div style="margin-top:10px">${[...p.roles].reverse().map((r) => html`<div class="rline" style="grid-template-columns:1fr auto">
      <div><b>${r.title.split("→").pop().trim()}</b><div class="small muted">${r.org_name}${r.ticker ? html` · <span class="tick link" onClick=${() => go(`/c/${r.ticker}`)}>${r.ticker}</span>` : ""}</div></div>
      <span class="tiny muted num">${r.from.slice(0, 4)}–${r.to ? r.to.slice(0, 4) : "now"}</span></div>`)}</div>
    ${p.education?.length > 0 && html`<div class="section-h"><h2>Education</h2></div>${p.education.map((e) => html`<div class="small ink2">${e.org_name}, ${e.degree} <span class="muted">(${e.from}–${e.to})</span></div>`)}`}
    ${p.is_contact && html`<div style="margin-top:16px;display:flex;gap:8px"><button class="pbtn" onClick=${note}>Draft a note</button></div>`}
    ${draft && html`<div class="draft" contenteditable="true">${draft}</div><p class="tiny muted">A starting point. FINN never sends anything for you.</p>`}
  </div>`;
}

function AskSheet({ q, openPerson, close }) {
  const [r, setR] = useState(null);
  useEffect(() => {
    setR(null);
    api(`/api/ask?q=${encodeURIComponent(q)}`).then((res) => {
      if (res.type === "company") { close(); go(`/c/${res.ticker}`); return; }
      if (res.type === "person") { openPerson(res.id); return; }
      setR(res);
    }).catch((e) => setR({ type: "help", answer: e.message }));
  }, [q]);
  if (!r) return html`<div class="loading">Thinking about “${q}”…</div>`;
  return html`<div>
    <p class="tiny muted upper" style="margin:0">You asked</p>
    <h2>${r.answer}</h2>
    ${r.type === "paths" && html`<div>
      <div class="callout"><span class="upper">Who they are</span>
        <div class="person" style="cursor:pointer" onClick=${() => openPerson(r.person.id)}><${Avatar} p=${r.person} cls="target" /><div><div class="pname">${r.person.name}</div><div class="phead">${r.person.headline}</div></div></div>
        <${Timeline} roles=${r.person.roles} highlightOrgs=${r.paths.flatMap((p) => p.orgs || [])} /></div>
      ${r.paths.map((p, k) => html`<div class="callout"><span class="upper">${k === 0 ? "Best route" : `Route ${k + 1}`} · strength ${Math.round(p.score * 100)}</span><${PathView} path=${p} target=${r.person} onPerson=${openPerson} /><${EvidenceGraph} g=${p.graph} /></div>`)}
    </div>`}
    ${(r.type === "who_for_company" || r.type === "expertise") && html`<div class="card" style="margin-top:12px">
      <${PeopleList} people=${r.people.map((x) => ({ ...x, why: x.why || [`${x.years?.toFixed(0)} yrs: ${x.evidence?.slice(0, 2).join("; ")}`] }))} onPerson=${openPerson} />
    </div>`}
    ${r.type === "who_for_company" && html`<button class="pbtn ghost" style="margin-top:8px" onClick=${() => { close(); go(`/c/${r.ticker}`); }}>Open ${r.ticker} dossier</button>`}
  </div>`;
}

const CATS = [["results", "Results"], ["M&A", "M&A"], ["management_change", "Management change"], ["litigation", "Litigation"], ["capex", "Capex"],
  ["fund_raise", "Fund raise"], ["credit_rating", "Credit rating"], ["pledging", "Pledging"], ["insider_trading", "Insider trades"], ["dividend", "Dividend"],
  ["bonus_split", "Bonus / split"], ["press_release", "Press release"], ["earnings_call", "Earnings call"], ["investor_meetings", "Investor meetings"],
  ["postal_ballot", "Postal ballot"], ["agm_egm", "AGM / EGM"]];

function RulesSheet({ onSaved }) {
  const [r, setR] = useState(null);
  useEffect(() => { api("/api/rules").then(setR); }, []);
  if (!r) return html`<div class="loading">Loading…</div>`;
  const cycle = (k) => {
    const w = new Set(r.watch), m = new Set(r.mute);
    if (w.has(k)) { w.delete(k); m.add(k); } else if (m.has(k)) { m.delete(k); } else { w.add(k); }
    setR({ ...r, watch: [...w], mute: [...m] });
  };
  const save = async () => { await api("/api/rules", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(r) }); onSaved(); };
  return html`<div>
    <h2>What should interrupt you?</h2>
    <${PhoneAlerts} />
    <p class="small muted" style="margin:0">Tap to cycle: <b>alert</b> → <s>muted</s> → neutral.</p>
    <div class="rule-group"><div class="toggles">${CATS.map(([k, l]) => html`<button class="toggle ${r.watch.includes(k) ? "on" : r.mute.includes(k) ? "mute" : ""}" onClick=${() => cycle(k)}>${l}</button>`)}</div></div>
    <div class="rule-group"><div class="upper muted">Unusual price move</div>
      <div class="range"><input type="range" min="1" max="10" step="0.5" value=${r.price_move_pct} onInput=${(e) => setR({ ...r, price_move_pct: +e.target.value })} /><b class="num">±${r.price_move_pct}%</b></div></div>
    <div class="rule-group"><div class="upper muted">Unusual volume</div>
      <div class="range"><input type="range" min="1.2" max="5" step="0.1" value=${r.volume_x} onInput=${(e) => setR({ ...r, volume_x: +e.target.value })} /><b class="num">${(+r.volume_x).toFixed(1)}× avg</b></div></div>
    <div class="rule-group"><label class="small" style="display:flex;gap:8px;align-items:center"><input type="checkbox" checked=${r.include_simulated} onChange=${(e) => setR({ ...r, include_simulated: e.target.checked })} />Include simulated demo scenarios</label></div>
    <button class="pbtn" onClick=${save}>Save rules</button>
  </div>`;
}

// ---------------------------------------------------------------------------
// Evidence graph: You → person → bridge → target (why this person fits)
// ---------------------------------------------------------------------------
function EvidenceGraph({ g }) {
  const [ref, W] = useWidth();
  if (!g?.nodes?.length) return null;
  const X = [0.07, 0.31, 0.6, 0.87].map((f) => f * W);
  const narrow = W < 330;
  const cols = [0, 1, 2, 3].map((c) => g.nodes.filter((n) => n.col === c));
  const H = Math.max(118, 62 * Math.max(...cols.map((c) => c.length), 1) + 34);
  const pos = {};
  cols.forEach((ns, c) => ns.forEach((n, i) => { pos[n.id] = [X[c], (H - 26) / (ns.length + 1) * (i + 1)]; }));
  const fill = { you: "var(--ink)", person: "var(--surface)", org: "var(--chip)", theme: "var(--sim-soft)", target: "var(--accent-soft)" };
  const stroke = { you: "var(--ink)", person: "var(--ink-2)", org: "var(--line-2)", theme: "var(--sim)", target: "var(--accent)" };
  const cut = (s, n) => (s && s.length > n ? s.slice(0, n - 1) + "…" : s || "");
  return html`<div ref=${ref}><svg class="egraph" width=${W} height=${H} viewBox="0 0 ${W} ${H}" role="img" aria-label="Evidence graph">
    ${g.edges.map((e) => {
      const a = pos[e.from], b = pos[e.to];
      if (!a || !b) return null;
      const skip = Math.abs(b[0] - a[0]) > 130;  // edge that jumps a column arcs over it
      const cy = skip ? Math.min(a[1], b[1]) - 30 : null;
      const mx = (a[0] + b[0]) / 2;
      const d = skip ? `M${a[0]},${a[1]} Q${mx},${cy} ${b[0]},${b[1]}` : `M${a[0]},${a[1]} C${mx},${a[1]} ${mx},${b[1]} ${b[0]},${b[1]}`;
      const ly = skip ? (a[1] + b[1]) / 2 - 16 - Math.abs(a[1] - b[1]) / 4 : (a[1] + b[1]) / 2 - 5;
      return html`<g><path d=${d} fill="none" stroke=${e.to === "target" || e.from === "you" ? "var(--ink-2)" : "var(--ink-3)"} stroke-opacity="0.6" stroke-width=${1 + 2.6 * Math.min(1, e.w || 0.3)} />
        ${e.label && html`<text x=${mx} y=${ly} text-anchor="middle" class="elabel">${cut(e.label, narrow ? 18 : 24)}</text>`}</g>`;
    })}
    ${g.nodes.map((n) => {
      const [x, y] = pos[n.id];
      const r = n.kind === "org" || n.kind === "theme" ? 8 : 12;
      return html`<g>
        <circle cx=${x} cy=${y} r=${r} fill=${fill[n.kind]} stroke=${stroke[n.kind]} stroke-width="1.6" />
        <text x=${x} y=${y + r + 13} text-anchor="middle" class="nlabel">${cut(n.label, narrow ? 12 : 15)}</text>
        ${n.sub && html`<text x=${x} y=${y + r + 25} text-anchor="middle" class="nsub">${cut(n.sub, narrow ? 14 : 18)}</text>`}
      </g>`;
    })}
  </svg></div>`;
}

function WhyPerson({ rec }) {
  const [sent, setSent] = useState(null);
  const cs = rec.contributions || [];
  const max = Math.max(...cs.map((c) => c.value), 0.01);
  const fb = async (label) => {
    try {
      const r = await api("/api/network/feedback", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ person_id: rec.person.id, query: rec.query, label }) });
      setSent({ label, n: r.n_feedback });
    } catch (e) { setSent({ err: e.message }); }
  };
  return html`<div class="whyp" onClick=${(e) => e.stopPropagation()}>
    <${EvidenceGraph} g=${rec.graph} />
    <div class="contrib">${cs.slice(0, 5).map((c) => html`<div class="crow"><span>${c.label}</span><div class="bar"><i style=${`width:${Math.round((c.value / max) * 100)}%`}></i></div><b class="num">${c.value.toFixed(1)}</b></div>`)}</div>
    ${rec.call_value ? html`<div class="tiny muted">Worth a call today: can help ${Math.round(rec.call_value.relevance * 100)}% × likely to pick up ${Math.round(rec.call_value.reachable * 100)}% × knowledge fresh ${Math.round(rec.call_value.fresh * 100)}%${rec.would_know ? `. Would know: ${rec.would_know}` : ""}.</div>`
      : html`<div class="tiny muted">Match ${Math.round(rec.score * 100)}%: a learned ranking over these signals${rec.would_know ? `. Would know: ${rec.would_know}` : ""}.</div>`}
    ${sent ? html`<div class="small ink2" style="margin-top:8px">${sent.err || (sent.label ? "Noted. Matches like this will rank higher for you." : "Noted. Matches like this will rank lower for you.")}</div>`
      : html`<div class="fb"><button class="pbtn ghost" onClick=${() => fb(1)}>Helpful</button><button class="pbtn ghost" onClick=${() => fb(0)}>Not relevant</button></div>`}
  </div>`;
}

function RecRow({ x, onPerson, compact }) {
  const [open, setOpen] = useState(false);
  return html`<div>
    <div class="person" style="cursor:pointer" onClick=${() => onPerson(x.person.id)}>
      <${Avatar} p=${x.person} />
      <div style="flex:1;min-width:0">
        <div style="display:flex;justify-content:space-between;gap:8px"><span class="pname">${x.person.name}</span><${Warmth} w=${x.warmth} /></div>
        <div class="phead">${x.why?.[0] || x.person.headline}</div>
        ${!compact && x.would_know && html`<div class="small ink2" style="margin-top:3px">Would know: ${x.would_know}</div>`}
      </div>
    </div>
    ${x.contributions && html`<button class="linkish tiny whybtn" onClick=${() => setOpen(!open)}>${open ? "Hide" : `Why ${x.person.name.split(" ")[0]}? · ${Math.round(x.score * 100)}% match`}</button>`}
    ${open && html`<${WhyPerson} rec=${x} />`}
  </div>`;
}

// ---------------------------------------------------------------------------
// Ask FINN: the agent chat
// ---------------------------------------------------------------------------
function md(text) {
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const inline = (s) => esc(s)
    .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<i>$2</i>").replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\[(E\d+(?:\s*,\s*E?\d+)*)\]/g, (_, ids) => ids.split(/\s*,\s*/).map((id) => {
      const k = id.startsWith("E") ? id : "E" + id;
      return `<button class="cite" data-e="${k}" aria-label="source ${k}">${k.slice(1)}</button>`;
    }).join(""));
  let out = "", list = false, table = [];
  const flushTable = () => {
    if (!table.length) return;
    const rows = table.filter((r) => !/^\|[\s:|-]+\|$/.test(r.trim())).map((r) => r.trim().replace(/^\||\|$/g, "").split("|"));
    out += `<table class="minitable">${rows.map((r, i) => `<tr>${r.map((c) => `<${i ? "td" : "th"}>${inline(c.trim())}</${i ? "td" : "th"}>`).join("")}</tr>`).join("")}</table>`;
    table = [];
  };
  for (const l of text.split("\n")) {
    if (l.trim().startsWith("|")) { table.push(l); continue; }
    flushTable();
    const m = l.match(/^\s*(?:[-*•]|\d+\.)\s+(.*)/);
    if (m) { if (!list) { out += "<ul>"; list = true; } out += `<li>${inline(m[1])}</li>`; continue; }
    if (list) { out += "</ul>"; list = false; }
    if (/^#{1,4}\s/.test(l)) { out += `<h4>${inline(l.replace(/^#+\s*/, ""))}</h4>`; continue; }
    if (l.trim()) out += `<p>${inline(l)}</p>`;
  }
  flushTable();
  if (list) out += "</ul>";
  return out;
}

async function streamChat(body, onEvent) {
  const r = await fetch("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) throw new Error(`${r.status}`);
  const reader = r.body.getReader(), dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, i);
      buf = buf.slice(i + 2);
      if (chunk.startsWith("data: ")) onEvent(JSON.parse(chunk.slice(6)));
    }
  }
}

function applyEvent(r, ev) {
  r = { ...r };
  if (ev.type === "node") {
    const nodes = [...r.nodes];
    if (ev.status === "start") nodes.push({ node: ev.node, label: ev.label, status: "running" });
    else {
      const k = nodes.map((n) => n.node).lastIndexOf(ev.node);
      if (k >= 0) nodes[k] = { ...nodes[k], status: ev.status === "error" ? "error" : "end", ms: ev.ms, detail: ev.detail };
    }
    r.nodes = nodes;
  } else if (ev.type === "plan") { r.plan = ev.steps; r.planSource = ev.source; }
  else if (ev.type === "step") r.steps = { ...r.steps, [ev.id]: ev };
  else if (ev.type === "evidence") r.evidence = ev.items;
  else if (ev.type === "answer") r.answer = ev;
  else if (ev.type === "verify") r.verify = ev;
  else if (ev.type === "memory") r.learned = ev.learned;
  else if (ev.type === "error") r.error = ev.error;
  return r;
}

const FLOW = [["understand", "Understand"], ["plan", "Plan"], ["execute", "Gather"], ["answer", "Answer"], ["verify", "Verify"], ["remember", "Remember"]];
const argsLabel = (a) => Object.entries(a || {}).filter(([k]) => k !== "query").map(([, v]) => v).join(" · ") + (a?.query ? ` “${a.query.slice(0, 48)}${a.query.length > 48 ? "…" : ""}”` : "");

function RunTrace({ r, open, toggle }) {
  const last = {}, count = {};
  r.nodes.forEach((n) => { last[n.node] = n; count[n.node] = (count[n.node] || 0) + 1; });
  const summary = r.done
    ? [r.plan && `${r.plan.length}-step plan`, r.evidence?.length && `${r.evidence.length} sources`,
       r.verify && (r.verify.figures ? `${r.verify.supported}/${r.verify.figures} figures verified` : "nothing to verify"),
       r.ms && `${(r.ms / 1000).toFixed(1)}s`].filter(Boolean).join(" · ")
    : (r.nodes.length ? `${r.nodes[r.nodes.length - 1].label}…` : "Starting…");
  return html`<div class="run">
    <button class="run-head" onClick=${toggle} aria-expanded=${open}>
      <span class="flow">${FLOW.map(([k, l], i) => html`${i ? html`<span class="arrow">›</span>` : ""}<span class="fnode ${last[k]?.status || "idle"}" title=${last[k]?.detail || ""}>${l}${count[k] > 1 ? html`<sup>×${count[k]}</sup>` : ""}</span>`)}</span>
      <span class="tiny muted">${summary}</span>
    </button>
    ${open && html`<div class="run-body">
      ${r.nodes.map((n) => html`<div class="rn"><span class="rdot ${n.status}"></span><b>${n.label}</b> <span class="muted">${n.detail || (n.status === "running" ? "…" : "")}</span>${n.ms != null ? html` <span class="tiny muted num">${n.ms} ms</span>` : ""}</div>`)}
      ${r.plan && html`<div class="steps"><div class="tiny muted upper" style="margin-bottom:4px">Plan · ${r.planSource === "model" ? "by the model" : "rule planner"}</div>${r.plan.map((st) => {
        const s = r.steps[st.id] || {};
        return html`<div class="step"><span class="rdot ${s.status === "done" ? "end" : s.status === "error" ? "error" : "running"}"></span><code>${st.tool}</code> <span class="muted small">${argsLabel(st.args)}</span>
          ${s.status === "done" ? html` <span class="tiny muted">${s.n} item${s.n === 1 ? "" : "s"}</span>` : s.status === "error" ? html` <span class="tiny neg">${s.error}</span>` : ""}
          ${st.why && html`<div class="tiny muted" style="margin-left:15px">${st.why}</div>`}</div>`;
      })}</div>`}
    </div>`}
  </div>`;
}

const SRC = { primary: "Primary", computed: "Computed", market: "Market", network: "Your network", secondary: "Secondary · web" };

function EvidenceCard({ e, onClose, openPerson }) {
  return html`<div class="evcard">
    <div class="meta"><span class="src ${e.source}">${SRC[e.source] || e.source}</span><b>${e.id}</b><span>${e.cite}</span>
      ${e.url && html`<a class="linkish" href=${e.url} target="_blank" rel="noopener">open ↗</a>`}
      <button class="linkish" style="margin-left:auto" onClick=${onClose}>close</button></div>
    <div class="evtitle">${e.title}</div>
    <div class="evtext">${e.text}</div>
    ${e.graph && html`<${EvidenceGraph} g=${e.graph} />`}
    ${e.person && html`<button class="pbtn ghost" style="margin-top:6px" onClick=${() => openPerson(e.person.id)}>Open ${e.person.name}</button>`}
  </div>`;
}

function Turn({ r, onFollow, openPerson }) {
  const [openEv, setOpenEv] = useState(null);
  const [trace, setTrace] = useState(true);
  const [srcOpen, setSrcOpen] = useState(false);
  useEffect(() => { if (r.done) setTrace(false); }, [r.done]);
  const ev = (r.evidence || []).find((x) => x.id === openEv);
  const people = (r.evidence || []).filter((x) => x.source === "network" && x.graph);
  const counts = (r.evidence || []).reduce((m, x) => ({ ...m, [x.source]: (m[x.source] || 0) + 1 }), {});
  return html`<div class="turn">
    <div class="bubble">${r.q}</div>
    <${RunTrace} r=${r} open=${trace} toggle=${() => setTrace(!trace)} />
    ${r.answer && html`<div class="answer">
      <div class="md" onClick=${(e) => { const b = e.target.closest(".cite"); if (b) setOpenEv(b.dataset.e === openEv ? null : b.dataset.e); }}
        dangerouslySetInnerHTML=${{ __html: md(r.answer.text) }}></div>
      ${ev && html`<${EvidenceCard} e=${ev} onClose=${() => setOpenEv(null)} openPerson=${openPerson} />`}
      ${people.length > 0 && html`<div class="people-strip">${people.slice(0, 3).map((p) => html`<button class="pchip" onClick=${() => setOpenEv(p.id)}><${Avatar} p=${p.person || { name: p.title.replace(/^.*?: /, "") }} /><span>${(p.person?.name || p.title).split(" ")[0]}</span></button>`)}<span class="tiny muted">tap to see why</span></div>`}
      <div class="answer-meta">
        ${r.verify && html`<span class=${`vbadge ${r.verify.ok ? "ok" : "warn"}`} title=${r.verify.ok ? "" : [...r.verify.unsupported, ...r.verify.uncited].join(" · ")}>
          ${r.verify.ok ? "✓" : "!"} ${r.verify.figures ? `${r.verify.supported}/${r.verify.figures} figures traced to sources` : "grounded in sources"}${r.verify.ok ? "" : ` · ${r.verify.unsupported.length + r.verify.uncited.length} to check`}</span>`}
        <span class="chip">${r.answer.confidence} confidence</span>
        <span class="tiny muted">${r.answer.mode === "model" ? "written from evidence" : "extractive · model offline"}</span>
      </div>
      <button class="linkish small" onClick=${() => setSrcOpen(!srcOpen)}>${srcOpen ? "Hide" : "Show"} ${r.evidence?.length || 0} sources</button>
      <span class="tiny muted"> ${Object.entries(counts).map(([k, v]) => `${v} ${SRC[k]?.toLowerCase() || k}`).join(" · ")}</span>
      ${srcOpen && html`<div class="srclist">${r.evidence.map((x) => html`<button class="srcrow" onClick=${() => setOpenEv(x.id)}><span class="src ${x.source}">${x.id}</span><span>${x.title}</span><span class="tiny muted">${x.cite}</span></button>`)}</div>`}
      ${r.answer.follow_ups?.length > 0 && r.done && html`<div class="suggest wrap">${r.answer.follow_ups.map((f) => html`<button onClick=${() => onFollow(f)}>${f}</button>`)}</div>`}
      ${r.learned?.length > 0 && html`<div class="tiny muted" style="margin-top:6px">Remembered for next time: ${r.learned.join("; ")}</div>`}
    </div>`}
    ${r.error && html`<div class="err" style="margin:8px 0">${r.error}</div>`}
  </div>`;
}

function ChatView({ params, openPerson }) {
  const ctx = useMemo(() => (params.t ? { ticker: params.t.toUpperCase(), title: params.title || null } : null), []);
  const [turns, setTurns] = useState([]);
  const [busy, setBusy] = useState(false);
  const [input, setInput] = useState("");
  const [info, setInfo] = useState(null);
  const tid = useRef(null);
  const endRef = useRef(null);
  useEffect(() => { api("/api/agent").then(setInfo).catch(() => {}); }, []);
  const send = async (q) => {
    q = (q || "").trim();
    if (!q || busy) return;
    setBusy(true); setInput("");
    setTurns((ts) => [...ts, { q, nodes: [], plan: null, steps: {}, evidence: [], answer: null, verify: null, done: false, t0: Date.now() }]);
    const update = (fn) => setTurns((ts) => ts.map((r, i) => (i === ts.length - 1 ? fn(r) : r)));
    try {
      await streamChat({ message: q, thread_id: tid.current, context: ctx }, (ev) => {
        if (ev.type === "thread") { tid.current = ev.id; return; }
        update((r) => applyEvent(r, ev));
      });
    } catch (e) { update((r) => ({ ...r, error: `Couldn't reach FINN (${e.message})` })); }
    update((r) => ({ ...r, done: true, ms: Date.now() - r.t0 }));
    setBusy(false);
  };
  useEffect(() => { if (params.q) send(params.q); }, []);
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" }); }, [turns.length, turns[turns.length - 1]?.answer]);
  const t = ctx?.ticker;
  const sugg = t ? (ctx.title ? [`What does this mean for ${t}?`, `What has ${t}'s management said about this before?`, `Who in my network can explain this?`]
                              : [`How is ${t} doing this quarter, and why?`, `What did ${t}'s management promise, and did they deliver?`, `Who in my network can explain ${t}?`, `What's the bear case on ${t}?`])
    : ["How is Dixon's margin holding up, and why?", "Who can intro me to the new CFO of Deepak Nitrite?", "What changed across my portfolio this week?", "Who knows semiconductors?"];
  return html`<div class="chat">
    <div class="topbar"><button class="iconbtn" onClick=${() => (history.length > 1 ? history.back() : go("/"))}>${Icon.back}</button>
      <div class="wordmark" style="font-size:19px">Ask FINN ${t && html`<span class="tick">${t}</span>`}</div>
      <button class="iconbtn" title="New conversation" onClick=${() => { tid.current = null; setTurns([]); }}>${Icon.plus}</button></div>
    ${ctx?.title && html`<div class="pinned">About: ${ctx.title}</div>`}
    ${!turns.length && html`<div class="section">
      <div class="hero" style="padding:4px 0 10px"><h1 style="font-size:28px">Ask anything. It shows its work.</h1>
        <p>FINN plans which sources to read, gathers evidence from filings, transcripts, financials, your network${info?.tools?.some((x) => x.name === "web_search") ? " and the web" : ""}, then answers with every figure traced to a source.</p></div>
      ${info && html`<div class="card"><span class="upper muted">How FINN answers</span><${AgentGraph} g=${info.graph} /></div>`}
      <div class="suggest wrap">${sugg.map((s) => html`<button onClick=${() => send(s)}>${s}</button>`)}</div>
      ${info?.facts?.length > 0 && html`<div class="card"><span class="upper muted">What FINN remembers about you</span>
        ${info.facts.map((f) => html`<div class="rline" style="grid-template-columns:1fr auto"><span class="small">${f.text}</span>
          <button class="linkish tiny" onClick=${async () => setInfo({ ...info, ...(await api(`/api/agent/memory/${f.id}`, { method: "DELETE" })) })}>forget</button></div>`)}</div>`}
      ${info && !info.model.enabled && html`<p class="tiny muted">No model key configured: FINN runs its rule planner and quotes the sources instead of writing prose.</p>`}
    </div>`}
    <div class="section">${turns.map((r, i) => html`<${Turn} key=${i} r=${r} onFollow=${send} openPerson=${openPerson} />`)}<div ref=${endRef}></div></div>
    <div class="dock"><div class="dock-inner">
      <form class="ask" onSubmit=${(e) => { e.preventDefault(); send(input); }}>
        <input value=${input} onInput=${(e) => setInput(e.target.value)} placeholder=${turns.length ? "Ask a follow-up" : "Ask FINN anything"} aria-label="Message FINN" enterkeyhint="send" autocomplete="off" disabled=${busy} />
        <button class="go" type="submit" aria-label="Send" disabled=${busy}>${busy ? html`<span class="spin"></span>` : Icon.arrow}</button>
      </form>
    </div></div>
  </div>`;
}

function AgentGraph({ g }) {
  // Linear spine with the two loops drawn as labelled return arcs.
  const order = ["understand", "plan", "execute", "answer", "verify", "remember"];
  const [ref, W] = useWidth();
  const y = 44, step = (W - 52) / (order.length - 1);
  const x = (id) => 26 + order.indexOf(id) * step;
  const label = Object.fromEntries(g.nodes.map((n) => [n.id, n.label]));
  const loops = g.edges.filter((e) => order.indexOf(e.to) >= 0 && order.indexOf(e.to) < order.indexOf(e.from));
  return html`<div ref=${ref}><svg class="egraph" width=${W} height="92" viewBox="0 0 ${W} 92" role="img" aria-label="Agent graph">
    ${order.slice(1).map((id, i) => html`<line x1=${x(order[i]) + 9} y1=${y} x2=${x(id) - 9} y2=${y} stroke="var(--ink-3)" stroke-width="1.4" />`)}
    ${loops.map((e) => { const a = x(e.from), b = x(e.to); return html`<g><path d=${`M${a},${y - 9} Q${(a + b) / 2},${y - 44} ${b},${y - 9}`} fill="none" stroke="var(--accent)" stroke-dasharray="3 3" stroke-width="1.2" />
      <text x=${(a + b) / 2} y=${y - 30} text-anchor="middle" class="elabel" style="fill:var(--accent)">${e.label.split(":")[1]?.trim() || e.label}</text></g>`; })}
    ${order.map((id) => html`<g><circle cx=${x(id)} cy=${y} r="8" fill="var(--surface)" stroke="var(--ink)" stroke-width="1.6" />
      <text x=${x(id)} y=${y + 24} text-anchor="middle" class="nlabel">${label[id]}</text></g>`)}
  </svg></div>`;
}

// ---------------------------------------------------------------------------
// Ask dock + App
// ---------------------------------------------------------------------------
const SUGGESTIONS = ["How is Dixon's margin holding up, and why?", "Who can intro me to the new CFO of Deepak Nitrite?", "NAVINFLUOR", "Who should I call about Laurus Labs?", "What did PI Industries guide for FY27?", "SRF"];

function Dock({ tab, onAsk }) {
  const [q, setQ] = useState("");
  const [focus, setFocus] = useState(false);
  const submit = (text) => { const t = (text ?? q).trim(); if (!t) return; onAsk(t); setQ(""); setFocus(false); document.activeElement?.blur(); };
  return html`<div class="dock"><div class="dock-inner">
    ${focus && html`<div class="suggest">${SUGGESTIONS.map((s) => html`<button onMouseDown=${(e) => { e.preventDefault(); submit(s); }}>${s}</button>`)}</div>`}
    <form class="ask" onSubmit=${(e) => { e.preventDefault(); submit(); }}>
      <input value=${q} onInput=${(e) => setQ(e.target.value)} onFocus=${() => setFocus(true)} onBlur=${() => setTimeout(() => setFocus(false), 120)}
        placeholder="Ask FINN: a ticker, a theme, a person" aria-label="Ask FINN" enterkeyhint="go" autocomplete="off" />
      <button class="go" type="submit" aria-label="Ask">${Icon.arrow}</button>
    </form>
    <nav class="tabs">${[["/", "brief", "Brief"], ["/portfolio", "portfolio", "Portfolio"], ["/network", "network", "Network"]].map(([p, k, l]) =>
      html`<button class=${tab === k ? "on" : ""} onClick=${() => go(p)}>${Icon[k]}<span>${l}</span></button>`)}</nav>
  </div></div>`;
}

function useRoute() {
  const [r, setR] = useState(location.hash.slice(1) || "/");
  useEffect(() => { const f = () => setR(location.hash.slice(1) || "/"); addEventListener("hashchange", f); return () => removeEventListener("hashchange", f); }, []);
  return r;
}

function App() {
  const route = useRoute();
  const [sheet, setSheet] = useState(null);
  const [rulesVersion, setRulesVersion] = useState(0);
  const openPerson = (id, ctx) => setSheet({ kind: "person", id, ctx });
  // One word (a ticker or a name) is a lookup; anything longer is a question for the agent.
  const openAsk = (q) => (/^\s*[A-Za-z0-9&.\-]{2,20}\s*$/.test(q) ? setSheet({ kind: "ask", q }) : go(`/chat?q=${encodeURIComponent(q)}`));
  const close = () => setSheet(null);
  const m = route.match(/^\/c\/([A-Za-z0-9&\-]+)/);
  const chat = route.startsWith("/chat");
  const tab = route.startsWith("/portfolio") ? "portfolio" : route.startsWith("/network") ? "network" : m || chat ? "" : "brief";
  return html`<div class="app">
    ${chat ? html`<${ChatView} key=${route} params=${Object.fromEntries(new URLSearchParams(route.split("?")[1] || ""))} openPerson=${openPerson} />`
      : m ? html`<${DossierView} ticker=${m[1].toUpperCase()} openPerson=${openPerson} />`
      : tab === "portfolio" ? html`<${PortfolioView} />`
      : tab === "network" ? html`<${NetworkView} openPerson=${openPerson} openAsk=${openAsk} />`
      : html`<${BriefView} openPerson=${openPerson} openRules=${() => setSheet({ kind: "rules" })} rulesVersion=${rulesVersion} />`}
    ${!chat && html`<${Dock} tab=${tab} onAsk=${openAsk} />`}
    ${sheet && html`<${Sheet} onClose=${close}>
      ${sheet.kind === "person" && html`<${PersonSheet} id=${sheet.id} ctx=${sheet.ctx} openPerson=${openPerson} />`}
      ${sheet.kind === "ask" && html`<${AskSheet} q=${sheet.q} openPerson=${openPerson} close=${close} />`}
      ${sheet.kind === "rules" && html`<${RulesSheet} onSaved=${() => { close(); setRulesVersion((v) => v + 1); }} />`}
    <//>`}
  </div>`;
}

registerSW();
render(html`<${App} />`, document.getElementById("app"));
