// FINN: mobile-first investor agent. Preact + htm, no build step.
import { html, render, useState, useEffect, useRef, useMemo, useCallback } from "/static/vendor/preact-htm.module.js";
import { disablePush, enablePush, haptic, pushState, registerSW, useInstall, useOnline, usePullToRefresh, useSheetDrag, useSwipe } from "/static/mobile.js";

// ---------------------------------------------------------------------------
// The visitor's own model: any provider, their key, the models they pick. Kept on this device only
// and sent with each request to FINN's API, which uses it for that request and never stores it.
// ---------------------------------------------------------------------------
const MK = "finn.model", MK_SKIP = "finn.model.skip";
const modelKey = {
  cfg: () => { try { const c = JSON.parse(localStorage.getItem(MK) || "null"); return c && c.key ? c : null; } catch { return null; } },
  get: () => modelKey.cfg()?.key || "",
  set: (c) => { try { localStorage.setItem(MK, JSON.stringify(c)); localStorage.removeItem(MK_SKIP); } catch {} },
  clear: () => { try { localStorage.removeItem(MK); } catch {} },
  skipped: () => { try { return localStorage.getItem(MK_SKIP) === "1"; } catch { return false; } },
  skip: () => { try { localStorage.setItem(MK_SKIP, "1"); } catch {} },
  label: (c) => `${c.label || c.provider} · ${c.deep}${c.fast !== c.deep ? ` + ${c.fast}` : ""} · key ••••${c.key.slice(-4)}`,
};
{
  const raw = window.fetch.bind(window);
  window.fetch = (input, init = {}) => {
    const url = typeof input === "string" ? input : input.url;
    const c = modelKey.cfg();
    if (c && (url.startsWith("/api") || url.startsWith(location.origin + "/api"))) {
      const h = new Headers(init.headers || (typeof input === "string" ? undefined : input.headers));
      h.set("X-FINN-Model-Key", c.key);
      h.set("X-FINN-Model-Provider", c.provider);
      if (c.fast) h.set("X-FINN-Model-Fast", c.fast);
      if (c.deep) h.set("X-FINN-Model-Deep", c.deep);
      if (c.base) h.set("X-FINN-Model-Base", c.base);
      init = { ...init, headers: h };
    }
    return raw(input, init);
  };
}

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
const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const first = (name) => (name || "").split(" ")[0];
const strengthBars = (s) => (s >= 0.6 ? 3 : s >= 0.35 ? 2 : 1);
// Sheets any screen can open (set by App on every render), so deep components needn't thread callbacks.
const ui = { openPerson: () => {}, openDraft: () => {}, openSheet: () => {}, me: null, radarChanged: () => {}, unread: 0, inboxChanged: () => {} };

// Numbers count up to their value instead of popping in.
function useCountUp(target, ms = 700) {
  const [v, setV] = useState(0);
  useEffect(() => {
    if (target == null || !isFinite(target)) return;
    const t0 = performance.now(), from = 0;
    let raf;
    const step = (t) => { const k = Math.min(1, (t - t0) / ms), e = 1 - Math.pow(1 - k, 3); setV(from + (target - from) * e); if (k < 1) raf = requestAnimationFrame(step); };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [target]);
  return v;
}

const svg = (body, w = 1.7) => html`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width=${w} stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;
const Icon = {
  radar: svg(html`<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="4.5"/><path d="M12 12l5.5-5.5"/><circle cx="12" cy="12" r="1" fill="currentColor"/>`),
  research: svg(html`<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5"/>`),
  search: svg(html`<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5"/>`),
  people: svg(html`<circle cx="9" cy="8.5" r="3.2"/><path d="M3.5 19.5c.8-3.2 3-5 5.5-5s4.7 1.8 5.5 5"/><circle cx="17" cy="9.5" r="2.5"/><path d="M15.8 14.6c2.2.3 3.8 1.9 4.4 4.9"/>`),
  chev: svg(html`<path d="M9 6l6 6-6 6"/>`, 2),
  ask: svg(html`<path d="M4.5 5.5h15v10h-9l-4.5 4v-4h-1.5z"/><path d="M8.5 10.5h7"/>`),
  draft: svg(html`<path d="M4 20h4L19.5 8.5l-4-4L4 16z"/><path d="M13.5 6.5l4 4"/>`),
  copy: svg(html`<rect x="8.5" y="8.5" width="11" height="11" rx="2"/><path d="M15.5 8.5V5.5a1 1 0 00-1-1h-9a1 1 0 00-1 1v9a1 1 0 001 1h3"/>`),
  arrow: html`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M5 12h13M13 6l6 6-6 6"/></svg>`,
  back: html`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M15 5l-7 7 7 7"/></svg>`,
  sliders: html`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2"/><circle cx="10" cy="17" r="2"/></svg>`,
  refresh: html`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M20 11a8 8 0 10-2.3 5.7M20 5v6h-6"/></svg>`,
  plus: html`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 5v14M5 12h14"/></svg>`,
  user: svg(html`<circle cx="12" cy="8.5" r="3.6"/><path d="M5 20c.9-3.6 3.7-5.6 7-5.6s6.1 2 7 5.6"/>`),
  bell: svg(html`<path d="M6 16.5V11a6 6 0 1112 0v5.5l1.5 2h-15z"/><path d="M10 20.5a2 2 0 004 0"/>`),
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
      ${path && html`<path d=${path} fill="none" stroke="var(--ink-2)" stroke-width="1.6" />`}
      ${pts.map((p, i) => p && (i === pts.length - 1 || i === 0) && html`<text x=${p[0]} y=${p[1] - 5} text-anchor="middle" style="fill:var(--ink-2)">${line[i].toFixed(1)}</text>`)}
      ${labels.map((l, i) => (i % Math.ceil(n / (W < 330 ? 4 : 6)) === 0 || i === n - 1) && html`<text x=${x(i) + bw / 2} y=${h - 5} text-anchor="middle">${shortPeriod(l)}</text>`)}
    </svg>
    <div class="legend"><span><i style="background:var(--line-2)"></i>${barLabel}</span>${line && html`<span><i style="background:var(--ink-2)"></i>${lineLabel}</span>`}</div>
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

function PeopleList({ people, onPerson, compact, draft }) {
  if (!people?.length) return html`<p class="small muted">No one in your network is close to this yet.</p>`;
  return html`<div class="stack">${people.map((x) => html`<${RecRow} key=${x.person.id} x=${x} onPerson=${onPerson} compact=${compact} draft=${draft} />`)}</div>`;
}

// ---------------------------------------------------------------------------
// Radar (home): the portfolio as a map, and the few holdings that need a look
// ---------------------------------------------------------------------------
const WINDOWS = ["24h", "3d", "7d"];
let radarCache = null;   // the holding page reads the radar's verdict for its ticker without a second fetch
const lsGet = (k) => { try { return localStorage.getItem(k); } catch { return null; } };
const lsSet = (k, v) => { try { localStorage.setItem(k, v); } catch {} };

// Squarified treemap (Bruls et al.): tiles close to square, so a 3% holding is still tappable.
function squarify(items, W, H) {
  const total = items.reduce((s, i) => s + i.value, 0) || 1;
  const nodes = items.map((i) => ({ ...i, area: (i.value / total) * W * H }));
  const out = [];
  let rect = { x: 0, y: 0, w: W, h: H }, row = [];
  const worst = (r, side) => {
    const s = r.reduce((a, n) => a + n.area, 0), mx = Math.max(...r.map((n) => n.area)), mn = Math.min(...r.map((n) => n.area));
    return Math.max((side * side * mx) / (s * s), (s * s) / (side * side * mn));
  };
  const place = (r) => {
    const s = r.reduce((a, n) => a + n.area, 0);
    if (rect.w >= rect.h) {
      const cw = s / rect.h; let y = rect.y;
      r.forEach((n) => { const h = n.area / cw; out.push({ ...n, x: rect.x, y, w: cw, h }); y += h; });
      rect = { x: rect.x + cw, y: rect.y, w: rect.w - cw, h: rect.h };
    } else {
      const rh = s / rect.w; let x = rect.x;
      r.forEach((n) => { const w = n.area / rh; out.push({ ...n, x, y: rect.y, w, h: rh }); x += w; });
      rect = { x: rect.x, y: rect.y + rh, w: rect.w, h: rect.h - rh };
    }
  };
  for (let i = 0; i < nodes.length;) {
    const side = Math.min(rect.w, rect.h);
    if (!row.length || worst([...row, nodes[i]], side) <= worst(row, side)) { row.push(nodes[i]); i++; }
    else { place(row); row = []; }
  }
  if (row.length) place(row);
  return out;
}

function Treemap({ tiles }) {
  const [sel, setSel] = useState(null);
  const [ref, W] = useWidth(343);
  const H = Math.round(Math.min(340, W * 0.82));
  const nodes = useMemo(() => squarify(tiles.filter((t) => t.weight > 0).sort((a, b) => b.weight - a.weight).map((t) => ({ ...t, value: t.weight })), W, H), [tiles, W, H]);
  return html`<div ref=${ref} class="tmap" style=${`height:${H}px`} role="list" aria-label="Your holdings, sized by weight">
    ${nodes.map((n, i) => {
      const c = n.change_pct || 0, mix = c ? Math.min(58, Math.abs(c) * 20) + 12 : 0;
      const bg = `color-mix(in oklab, var(${c >= 0 ? "--pos" : "--neg"}) ${mix}%, var(--surface))`;
      return html`<button role="listitem" style=${`left:${n.x}px;top:${n.y}px;width:${n.w}px;height:${n.h}px;background:${bg};animation-delay:${Math.min(i, 20) * 22}ms`}
        aria-label=${`${n.name}, ${sgn(c, 1)}${n.status ? ", needs a look" : ""}`} aria-pressed=${sel?.ticker === n.ticker}
        class=${`tile ${c > 0 ? "up" : c < 0 ? "down" : ""} ${n.status || ""} ${sel?.ticker === n.ticker ? "sel" : ""}`}
        onClick=${() => { haptic(8); sel?.ticker === n.ticker ? go(`/c/${n.ticker}`) : setSel(n); }}>
        ${n.status === "filing" && html`<i class="tdot" aria-hidden="true"></i>`}<span class="tt">${n.ticker}</span>${n.h >= 38 && html`<span class="tc">${sgn(c, 1)}</span>`}${n.h >= 58 && n.w >= 54 && html`<span class="tw">${n.weight}%</span>`}
      </button>`;
    })}
  </div>
  ${sel && html`<div class="peek" role="status">
    <div><div class="pk-name">${sel.name}</div><div class="phead"><span class="tick">${sel.ticker}</span>${sel.theme ? ` · ${sel.theme}` : ""}</div></div>
    <button class="btn sig" onClick=${() => go(`/c/${sel.ticker}`)}>Open ${Icon.chev}</button>
    <div class="pk-stats"><span>Today <b class=${tone(sel.change_pct)}>${sgn(sel.change_pct, 1)}</b></span><span>Weight <b>${sel.weight}%</b></span>
      ${sel.volume_x != null && html`<span>Volume <b>${sel.volume_x.toFixed(1)}×</b> avg</span>`}
      <span>${sel.status === "filing" ? "Filed something that matters" : sel.status === "move" ? "Moved without a filing" : "Quiet"}</span></div>
  </div>`}`;
}

function Glance({ tiles }) {
  const w = tiles.reduce((s, t) => s + (t.weight || 0), 0) || 1;
  const priced = tiles.filter((t) => t.change_pct != null);
  const move = priced.reduce((s, t) => s + (t.weight || 0) * t.change_pct, 0) / w;
  const up = priced.filter((t) => t.change_pct > 0).length, dn = priced.filter((t) => t.change_pct < 0).length;
  const best = [...priced].sort((a, b) => b.change_pct - a.change_pct)[0], worst = [...priced].sort((a, b) => a.change_pct - b.change_pct)[0];
  const m = useCountUp(move), u = useCountUp(up), d = useCountUp(dn);
  if (!priced.length) return null;
  return html`<div class="stats">
    <div class="stat"><div class="k">Portfolio</div><div class=${`v ${tone(move)}`}>${sgn(m, 2)}</div><div class="s">weighted, today</div></div>
    <div class="stat"><div class="k">Up · down</div><div class="v"><span class="pos">${Math.round(u)}</span> <span class="muted">·</span> <span class="neg">${Math.round(d)}</span></div>
      <div class="mbar"><i class="up" style=${`width:${(up / priced.length) * 100}%`}></i><i class="dn" style=${`width:${(dn / priced.length) * 100}%`}></i></div></div>
    <div class="stat"><div class="k">Movers</div>
      ${[best, worst].filter(Boolean).map((t) => html`<div class="mover"><span class="mt">${t.ticker}</span><b class=${tone(t.change_pct)}>${sgn(t.change_pct, 1)}</b></div>`)}</div>
  </div>`;
}

function NeedRow({ n }) {
  const open = () => go(`/c/${n.ticker}`);
  return html`<div class=${`nrow upd ${n.move ? "mv" : ""}`} role="link" tabindex="0" onClick=${open} onKeyDown=${(e) => e.key === "Enter" && open()}>
    <div class="need-meta"><span class="tick">${n.ticker}</span><span>${n.label}</span><span>·</span><span>${ago(n.ts)}</span>${n.simulated && html`<span class="badge sim">Simulated</span>`}</div>
    <div class="need-title">${n.company}: ${n.title}</div>
    ${n.hook ? html`<div class="need-hook"><${Avatar} p=${n.hook.person} cls="sm" /><span>${n.hook.text}</span></div>`
      : n.move ? html`<div class="need-hook"><b class="delta ${tone(n.move.change_pct)}">${sgn(n.move.change_pct, 1)}</b><span>${n.move.volume_x?.toFixed(1)}× volume · ${n.line}</span></div>` : ""}
    <span class="need-chev" aria-hidden="true">${Icon.chev}</span>
  </div>`;
}

function Swipeable({ onSwipe, children }) {
  const ref = useSwipe(onSwipe);
  return html`<div class="swipe-wrap"><div class="swipe-under" aria-hidden="true"><span>Handled ✓</span><span>Handled ✓</span></div><div ref=${ref} class="swipe">${children}</div></div>`;
}

function RadarView({ openRules, rulesVersion }) {
  const [win, setWin] = useState(() => (WINDOWS.includes(lsGet("finn.win")) ? lsGet("finn.win") : "24h"));
  const [d, setD] = useState(() => (radarCache?.window === win ? radarCache : null));
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const [offline, setOffline] = useState(false);
  const [toast, setToast] = useState(null);
  const online = useOnline();
  const inst = useInstall();
  const load = useCallback(async () => {
    setErr(null);
    try {
      const r = await fetch(`/api/radar?window=${win}`);
      if (!r.ok) throw new Error(r.status === 503 ? "You're offline and there's no saved radar yet." : `${r.status}`);
      setOffline(r.headers.get("X-FINN-Offline") === "1");
      const j = await r.json();
      radarCache = j; setD(j);
    } catch (e) { setErr(e.message); }
  }, [win]);
  useEffect(() => { lsSet("finn.win", win); load(); }, [win, rulesVersion]);
  const { pull, busy: pulling } = usePullToRefresh(load);
  const refresh = async () => { setBusy(true); try { await api("/api/refresh", { method: "POST" }); await load(); } catch {} finally { setBusy(false); } };
  // Swipe a holding away = "handled": every filing behind it is marked, on every device; undo for 5 seconds.
  const handle = useCallback(async (n) => {
    setD((x) => ({ ...x, needs: x.needs.filter((y) => y.ticker !== n.ticker) }));
    setToast(n);
    await Promise.all(n.ids.map((id) => post("/api/brief/dismiss", { id }).catch(() => {})));
    load();
  }, [load]);
  const undo = async () => {
    const n = toast; setToast(null);
    if (!n) return;
    await Promise.all(n.ids.map((id) => post("/api/brief/dismiss", { id, undo: true }).catch(() => {})));
    load();
  };
  useEffect(() => { if (!toast) return; const k = setTimeout(() => setToast(null), 5000); return () => clearTimeout(k); }, [toast]);
  const today = new Date().toLocaleDateString("en-IN", { weekday: "short", day: "numeric", month: "short" });
  const asOf = d?.as_of ? new Date(d.as_of).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" }) : "";
  return html`<div>
    <div class="ptr" style=${`height:${pull}px`}><span class=${pulling ? "spin-icon" : ""} style=${`opacity:${Math.min(1, pull / 64)};transform:rotate(${pull * 3}deg)`}>${Icon.refresh}</span></div>
    <div class="topbar">
      <div class="wordmark">FINN <small>${today}</small></div>
      <div style="display:flex;gap:2px">
        <button class="iconbtn" aria-label="Add a company to your radar" onClick=${() => ui.openSheet({ kind: "addco" })}>${Icon.plus}</button>
        <${Bell} />
        <button class="iconbtn" aria-label="Alert rules and notifications" onClick=${openRules}>${Icon.sliders}</button>
        <${AccountButton} />
      </div>
    </div>
    ${(!online || offline) && html`<div class="offline">Offline · showing your last radar${asOf ? ` from ${asOf}` : ""}</div>`}
    ${inst.canPrompt && html`<button class="installbar" onClick=${inst.install}><img src="/static/icons/icon-192.png" alt="" /><span><b>Install FINN</b><br /><span class="tiny">One tap from your home screen, alerts on your lock screen</span></span></button>`}
    ${inst.iosHint && html`<div class="installbar ios"><img src="/static/icons/icon-192.png" alt="" /><span><b>Add FINN to your Home Screen</b><br /><span class="tiny">Tap Share, then “Add to Home Screen”, to get alerts on iPhone</span></span><button class="linkish tiny" onClick=${(e) => { inst.hideHint(); e.currentTarget.parentElement.remove(); }}>Not now</button></div>`}
    ${err && html`<div class="err">Couldn't load the radar: ${err}</div>`}
    ${!d && !err && html`<div class="section"><div class="skeleton" style="height:60px;background:none;border:0;box-shadow:none"></div><div class="skeleton" style="height:280px"></div></div>`}
    ${d?.empty && html`<div class="section"><div class="empty-hero">
      <div class="ring">${Icon.radar}</div><h1 style="font-size:24px;margin:0 0 6px">${d.headline}</h1>
      <p class="small" style="margin:0 auto 16px;max-width:300px">${d.sub}</p>
      <button class="btn sig" onClick=${() => ui.openSheet({ kind: "addco" })}>${Icon.plus} Add your first company</button></div></div>`}
    ${d && !d.empty && html`<div>
      <div class="hero">
        <h1>${d.headline}</h1>
        <p>${d.sub}</p>
      </div>
      <div class="section" style="padding-top:0">
        <div class="row" style="align-items:center;margin-bottom:6px">
          <div class="seg" role="tablist" aria-label="Time window">${WINDOWS.map((w) => html`<button role="tab" aria-selected=${w === win} class=${w === win ? "on" : ""} onClick=${() => setWin(w)}>${w}</button>`)}</div>
          <span class="tiny muted">tap a tile to peek</span>
        </div>
        <${Glance} tiles=${d.tiles} />
        <${Treemap} tiles=${d.tiles} />
        <div class="legend2"><span><i class="lg-filing"></i>has an update</span><span><i class="lg-move"></i>moved, no filing</span><span><i class="lg-up"></i>up</span><span><i class="lg-dn"></i>down</span><span>size = weight</span></div>
      </div>
      ${d.needs.length > 0 && html`<div class="section">
        <div class="section-h"><h2>Needs a look</h2><span class="tiny muted">swipe when handled</span></div>
        ${d.needs.map((n) => html`<${Swipeable} key=${n.ticker} onSwipe=${() => handle(n)}><${NeedRow} n=${n} /><//>`)}
      </div>`}
      <div class="section">
        <div class="quiet">${d.quiet.count} holding${d.quiet.count === 1 ? "" : "s"} quiet${d.quiet.routine ? `, ${d.quiet.routine} routine filing${d.quiet.routine > 1 ? "s" : ""} folded away` : ""}.
          ${d.quiet.biggest && html` Biggest quiet move: <button class="linkish" onClick=${() => go(`/c/${d.quiet.biggest.ticker}`)}>${d.quiet.biggest.name} <span class="delta ${tone(d.quiet.biggest.change_pct)}">${sgn(d.quiet.biggest.change_pct, 1)}</span></button>.`}</div>
        <div class="caughtup">${d.filings} filing${d.filings === 1 ? "" : "s"} read for you${d.done ? ` · ${d.done} handled` : ""}${d.last_refresh?.at ? ` · sources refreshed ${ago(d.last_refresh.at)}` : ""}</div>
      </div>
    </div>`}
    ${toast && html`<div class="toast" role="status"><span>Handled: ${toast.company}</span><button onClick=${undo}>Undo</button></div>`}
  </div>`;
}
// ---------------------------------------------------------------------------
// Holding (Pull): updates, condition, network
// ---------------------------------------------------------------------------
const VERDICT = { delivered: "Delivered", missed: "Missed", partly: "Partly", on_track: "On track", behind: "Behind", pending: "Pending", unverifiable: "Can't check", mention: "Unverified" };

// Holding: open a tile and see, in this order, what changed, how the company is doing, and who can help.
function Fold({ title, hint, children, open: initial = false }) {
  const [open, setOpen] = useState(initial);
  return html`<div class="fold2 ${open ? "open" : ""}">
    <button class="fold2-h" aria-expanded=${open} onClick=${() => setOpen(!open)}><span>${title}</span><span class="tiny muted">${hint || ""} <i class="chev">${Icon.chev}</i></span></button>
    ${open && html`<div class="fold2-b">${children}</div>`}
  </div>`;
}

// The three things worth knowing first: the best computed positive, then the sharpest worries.
function thingsToKnow(d) {
  const seen = new Set(), pos = [], other = [];
  const add = (text, t) => { if (!text || seen.has(text)) return; seen.add(text); (t === "pos" ? pos : other).push({ text, tone: t || "neg" }); };
  (d.bull_bear?.bull || []).forEach((b) => add(b.point, "pos"));
  [...(d.trajectory?.flags || []), ...(d.cash?.flags || []), ...(d.balance_sheet?.flags || [])].forEach((f) => add(f.text, f.tone));
  const out = [...pos.slice(0, 1), ...other.filter((x) => x.tone === "neg").slice(0, 2)];
  for (const x of [...other, ...pos]) { if (out.length >= 3) break; if (!out.includes(x)) out.push(x); }
  return out.slice(0, 3);
}

function remember(t) {
  try { const r = JSON.parse(localStorage.getItem("finn.recent") || "[]").filter((x) => x !== t); r.unshift(t); localStorage.setItem("finn.recent", JSON.stringify(r.slice(0, 6))); } catch {}
}

function HookCard({ hook, ticker, eventId }) {
  if (!hook) return null;
  return html`<div class="hook">
    <span class="upper">Your best move</span>
    <div class="person" onClick=${() => ui.openPerson(hook.person.id)}><${Avatar} p=${hook.person} cls="need" />
      <div style="flex:1;min-width:0"><div class="pname">${hook.text}</div>${hook.because && html`<div class="phead clamp2">${hook.because}</div>`}</div></div>
    <button class="btn sig full" onClick=${() => ui.openDraft({ person: hook.person, intent: hook.intent, target: hook.target, ticker, event_id: eventId })}>${Icon.draft} Draft a message to ${first(hook.person.name)}</button>
  </div>`;
}

function HoldingView({ ticker, openPerson }) {
  const [d, setD] = useState(null);
  const [err, setErr] = useState(null);
  const [need, setNeed] = useState(() => radarCache?.needs?.find((n) => n.ticker === ticker) || null);
  const [tab, setTab] = useState(null);
  useEffect(() => {
    setD(null); setErr(null); setTab(null); window.scrollTo(0, 0); remember(ticker);
    api(`/api/company/${ticker}`).then(setD).catch((e) => setErr(e.message));
    const pick = (r) => setNeed(r?.needs?.find((n) => n.ticker === ticker) || null);
    if (radarCache) pick(radarCache);
    else api("/api/radar").then((r) => { radarCache = r; pick(r); }).catch(() => {});
  }, [ticker]);
  const back = html`<div class="topbar"><button class="iconbtn" aria-label="Back" onClick=${() => (history.length > 1 ? history.back() : go("/"))}>${Icon.back}</button>
    <span class="tick">${ticker}</span><button class="iconbtn" aria-label=${`Ask FINN about ${ticker}`} onClick=${() => go(`/chat?t=${ticker}`)}>${Icon.ask}</button></div>`;
  if (err) return html`<div>${back}<div class="err">${err}</div></div>`;
  if (!d) return html`<div>${back}<div class="loading">Reading ${ticker}'s filings and transcripts…<div style="margin-top:14px" class="skeleton"></div><div class="skeleton"></div></div></div>`;
  const fresh = d.cause && Date.now() - new Date(d.cause.filing.ts).getTime() < 7 * 864e5;
  const cur = tab || (need || fresh ? "updates" : "condition");
  const p = d.price;
  const pos52 = p?.high_52w > p?.low_52w ? Math.max(0, Math.min(100, ((p.close - p.low_52w) / (p.high_52w - p.low_52w)) * 100)) : null;
  return html`<div>
    ${back}
    <div class="hhead">
      <div class="meta">${[...new Set((d.sector_path || []).slice(1, 4))].slice(0, 2).join(" › ")}</div>
      <h1>${d.name}</h1>
      ${p && html`<div class="price-line"><span class="p">₹${num(p.close, 1)}</span><span class="delta ${tone(p.change_pct)}">${sgn(p.change_pct, 1)}</span><${Spark} data=${p.spark} w=${84} h=${26} />
        ${p.volume_x != null && html`<span class="tiny muted">vol ${p.volume_x.toFixed(1)}×</span>`}</div>`}
      ${pos52 != null && html`<div class="range52"><div class="rbar"><i style=${`left:${pos52}%`}></i></div><div class="row tiny muted"><span>52w low ${num(p.low_52w)}</span><span>high ${num(p.high_52w)}</span></div></div>`}
      <div class="hstatus">${d.in_portfolio ? html`<span class="chip">${d.weight}% of portfolio</span>` : html`<span class="chip">Research list</span>`}
        ${need ? html`<span class="badge need">Needs a look</span>` : html`<span class="tiny muted">Nothing urgent</span>`}
        <${RadarMembership} ticker=${ticker} name=${d.name} held=${d.in_portfolio} /></div>
    </div>
    <div class="seg3" role="tablist" aria-label="Holding sections">${[["updates", "Updates"], ["condition", "Condition"], ["network", "Network"]].map(([k, l]) =>
      html`<button role="tab" aria-selected=${cur === k} class=${cur === k ? "on" : ""} onClick=${() => setTab(k)}>${l}${k === "updates" && need ? html`<i class="sdot"></i>` : ""}</button>`)}</div>
    ${cur === "updates" && html`<${UpdatesPane} d=${d} need=${need} ticker=${ticker} openPerson=${openPerson} />`}
    ${cur === "condition" && html`<${ConditionPane} d=${d} ticker=${ticker} />`}
    ${cur === "network" && html`<${NetworkPane} d=${d} need=${need} ticker=${ticker} openPerson=${openPerson} />`}
  </div>`;
}

function UpdatesPane({ d, need, ticker, openPerson }) {
  const [all, setAll] = useState(false);
  const lead = need ? { title: need.title, line: need.line, label: need.label, ts: need.ts, url: need.url, simulated: need.simulated, tier: need.tier }
    : d.cause ? { title: d.cause.filing.title, line: d.cause.filing.summary, label: d.cause.filing.category_label, ts: d.cause.filing.ts, url: d.cause.filing.url, tier: "material" } : null;
  const filings = d.filings || [];
  const showResults = d.latest_results && lead && /result/i.test(lead.label || "");
  const qs2 = (q) => `/chat?t=${ticker}&q=${encodeURIComponent(q)}${lead ? `&title=${encodeURIComponent(`${d.name}: ${lead.title}`)}` : ""}`;
  return html`<div class="pane">
    ${lead ? html`<article class="card upd">
      <div class="meta"><span class="dot ${lead.tier}"></span><span>${need ? "Latest update" : "Last material update"}</span><span>·</span><span>${lead.label}</span><span>·</span><span>${ago(lead.ts)}</span>${lead.simulated && html`<span class="badge sim">Simulated</span>`}</div>
      <h3>${lead.title}</h3>
      ${lead.line && html`<p class="one">${lead.line}</p>`}
      ${showResults && html`<${ResultsGrid} r=${d.latest_results} />`}
      <div class="card-foot"><span>${need?.more ? `+${need.more} related filing${need.more > 1 ? "s" : ""}` : ""}</span>${lead.url ? html`<a href=${lead.url} target="_blank" rel="noopener">Read the filing ↗</a>` : html`<span>no source (simulated)</span>`}</div>
      ${need?.event_id && html`<${WebContext} i=${{ id: need.event_id, simulated: need.simulated, tier: need.tier }} openPerson=${openPerson} />`}
    </article>` : html`<div class="empty"><b>No material news lately</b><span>FINN reads ${ticker}'s exchange filings as they land and flags anything that matters on your radar.</span></div>`}
    <${HookCard} hook=${need?.hook} ticker=${ticker} eventId=${need?.event_id} />
    ${lead && html`<div class="suggest wrap">${[`What does this mean for ${ticker}?`, `What has ${ticker}'s management said about this before?`].map((q) => html`<button onClick=${() => go(qs2(q))}>${q}</button>`)}</div>`}
    ${filings.length > 0 && html`<div class="section-h"><h2>Earlier filings</h2><span class="tiny muted">${filings.length}</span></div>
      <div class="card list">${(all ? filings : filings.slice(0, 5)).map((f) => html`<a class="rline" href=${f.url} target="_blank" rel="noopener"><span class="dot ${f.tier}"></span>
        <div><div>${f.title}</div><div class="small muted">${f.category_label} · ${ago(f.ts)}</div></div></a>`)}
        ${filings.length > 5 && html`<button class="linkish small" onClick=${() => setAll(!all)}>${all ? "Show fewer" : `Show all ${filings.length}`}</button>`}</div>`}
  </div>`;
}

function ConditionPane({ d, ticker }) {
  const s = d.snapshot, L = d.ledger, sc = L.score || {};
  const things = thingsToKnow(d);
  const worry = things.find((t) => t.tone === "neg");
  const helper = d.cause?.people?.[0] || d.people?.[0];
  const bull = d.bull_bear.bull, bear = d.bull_bear.bear;
  return html`<div class="pane">
    ${things.length > 0 && html`<div class="section-h" style="margin-top:4px"><h2>${things.length === 1 ? "One thing" : `${things.length} things`} to know</h2><span class="tiny muted">${d.basis || ""}</span></div>
      <ol class="know">${things.map((t) => html`<li class=${`k-${t.tone}`}>${t.text}</li>`)}</ol>`}
    <div class="kv kv4">
      <div><span>Mkt cap</span><b>${num(s.market_cap_cr)}</b><small>₹ cr</small></div>
      <div><span>P/E</span><b>${num(s.pe, 1)}</b></div>
      <div><span>${s.lender ? "ROE" : "ROCE"}</span><b>${num(s.lender ? s.roe : s.roce, 1)}%</b></div>
      <div><span>${s.lender ? "Op. margin" : "EBITDA margin"}</span><b>${num(s.margin_ttm, 1)}%</b></div>
    </div>
    ${worry && html`<div class="ask-next">
      <span class="upper muted">Worth asking</span>
      <div class="q">What explains this: ${worry.text.replace(/\.$/, "")}?</div>
      <div class="row-btns"><button class="btn" onClick=${() => go(`/chat?t=${ticker}&q=${encodeURIComponent(`What explains this at ${d.name}: ${worry.text}?`)}`)}>${Icon.ask} Ask FINN</button>
        ${helper && html`<button class="btn" onClick=${() => ui.openDraft({ person: helper.person, intent: "view", ticker })}>${Icon.draft} Ask ${first(helper.person.name)}</button>`}</div>
    </div>`}
    <div style="margin-top:8px">
    <${Fold} title="Promises vs delivery" hint=${L.method === "heuristic" ? `${L.calls?.length || 0} calls, unverified` : `${sc.delivered || 0} kept · ${sc.missed || 0} missed`}>
      <p class="small muted" style="margin:0 0 8px">What management guided on the last ${L.calls?.length || 0} earnings calls, checked against what they then reported.</p>
      ${L.method === "heuristic" && html`<div class="banner">Pattern-matched from transcripts: these are guidance-shaped quotes, not verified promises.</div>`}
      ${L.method !== "heuristic" && html`<div class="scoreboard">
        <div><b class="pos">${sc.delivered || 0}</b><span>Delivered</span></div><div><b class="neg">${sc.missed || 0}</b><span>Missed</span></div>
        <div><b>${sc.on_track || 0}</b><span>On track</span></div><div><b class=${sc.behind ? "neg" : ""}>${sc.behind || 0}</b><span>Behind</span></div></div>`}
      ${L.observations?.length > 0 && html`<ul class="obs">${L.observations.map((o) => html`<li>${o}</li>`)}</ul>`}
      ${!L.items?.length && html`<p class="small muted">${L.calls?.length ? "No explicit numeric guidance found in these calls." : "No concall transcripts are published for this company."}</p>`}
      <div>${(L.items || []).map((it) => html`<${LedgerItem} it=${it} />`)}</div>
    <//>
    <${Fold} title="Growth and margins" hint=${`revenue 3y ${sgn(d.trajectory.cagr.revenue_3y, 0)}`}>
      ${d.business?.what_it_does && html`<p class="small ink2" style="margin:0 0 8px">${d.business.what_it_does}</p>`}
      ${d.business?.segments?.length > 0 && html`<div class="chips" style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px">${d.business.segments.map((g) => html`<span class="chip">${g.name}${g.share_pct != null ? html`<b>${g.share_pct}%</b>` : ""}</span>`)}</div>`}
      <div class="small muted">₹ crore. Revenue CAGR 3y <b class="num">${sgn(d.trajectory.cagr.revenue_3y, 0)}</b> · 5y <b class="num">${sgn(d.trajectory.cagr.revenue_5y, 0)}</b> · PAT 3y <b class="num">${sgn(d.trajectory.cagr.pat_3y, 0)}</b></div>
      <${BarLine} labels=${d.trajectory.years} bars=${d.trajectory.revenue} line=${d.trajectory.margin} barLabel="Revenue (annual)" lineLabel=${s.lender ? "Op. margin %" : "EBITDA margin %"} />
      <ul class="flags">${d.trajectory.flags.map((f) => html`<li class=${f.tone}>${f.text}</li>`)}</ul>
      <${BarLine} labels=${d.trajectory.quarters.periods} bars=${d.trajectory.quarters.revenue} line=${d.trajectory.quarters.margin} barLabel="Revenue (quarterly)" lineLabel="Margin %" h=${100} />
      ${d.latest_results && html`<div class="section-h"><h2>Latest quarter</h2></div><${ResultsGrid} r=${d.latest_results} />`}
    <//>
    <${Fold} title="Cash and debt" hint=${d.cash.flags.length + d.balance_sheet.flags.length ? `${d.cash.flags.length + d.balance_sheet.flags.length} flags` : "clean"}>
      <ul class="flags">${[...d.cash.flags, ...d.balance_sheet.flags].map((f) => html`<li class=${f.tone}>${f.text}</li>`)}</ul>
      <table class="minitable">
        <thead><tr><th></th>${d.cash.periods.slice(-5).map((x) => html`<th>${shortPeriod(x)}</th>`)}</tr></thead>
        <tbody>
          <tr><td>Cash from ops</td>${d.cash.cfo.slice(-5).map((v) => html`<td>${num(v)}</td>`)}</tr>
          <tr><td>PAT</td>${d.cash.pat.slice(-5).map((v) => html`<td>${num(v)}</td>`)}</tr>
          ${!d.cash.lender && html`<tr><td>CFO / PAT</td>${d.cash.conversion.slice(-5).map((v) => html`<td class=${v == null ? "" : v >= 90 ? "pos" : v < 70 ? "neg" : ""}>${v == null ? "–" : v + "%"}</td>`)}</tr>`}
          ${d.cash.fcf?.length > 0 && html`<tr><td>Free cash flow</td>${d.cash.fcf.slice(-5).map((v) => html`<td class=${tone(v)}>${num(v)}</td>`)}</tr>`}
        </tbody>
      </table>
      <table class="minitable" style="margin-top:12px">
        <thead><tr><th></th>${d.balance_sheet.periods.slice(-5).map((x) => html`<th>${shortPeriod(x)}</th>`)}</tr></thead>
        <tbody>
          <tr><td>Borrowings</td>${d.balance_sheet.borrowings.slice(-5).map((v) => html`<td>${num(v)}</td>`)}</tr>
          ${!d.balance_sheet.lender && html`<tr><td>Debt / equity</td>${d.balance_sheet.de.slice(-5).map((v) => html`<td>${v == null ? "–" : v.toFixed(2)}</td>`)}</tr>`}
        </tbody>
      </table>
    <//>
    <${Fold} title="Bull vs bear" hint=${`${bull.length} for · ${bear.length} against`}>
      <p class="small muted" style="margin:0 0 8px">${d.bull_bear.source === "model" ? "Argued by the deep model from the facts above only." : "Assembled from the computed signals above."}</p>
      <div class="bb">
        <div class="col bull"><h3>Bull</h3><ol>${bull.map((b) => html`<li>${b.point}<small>${b.evidence}</small></li>`)}</ol>${!bull.length && html`<p class="small muted">Nothing stands out.</p>`}</div>
        <div class="col bear"><h3>Bear</h3><ol>${bear.map((b) => html`<li>${b.point}<small>${b.evidence}</small></li>`)}</ol>${!bear.length && html`<p class="small muted">Nothing stands out.</p>`}</div>
      </div>
    <//>
    </div>
    <p class="tiny muted" style="margin-top:14px">Sources: ${L.calls?.length || 0} concall transcripts${L.calls?.length ? ` (${L.calls.join(", ")})` : ""}, exchange filings, reported financials (${d.basis}). Generated ${ago(d.generated_at)}.
      ${d.documents?.annual_reports?.[0] && html` <a class="linkish" href=${d.documents.annual_reports[0].url} target="_blank" rel="noopener">Annual report ${d.documents.annual_reports[0].year} ↗</a>`}</p>
  </div>`;
}

function NetworkPane({ d, need, ticker, openPerson }) {
  const eventId = need?.event_id;
  const short = d.name.replace(/\s+(Ltd\.?|Limited)$/i, "");
  return html`<div class="pane">
    <${HookCard} hook=${need?.hook} ticker=${ticker} eventId=${eventId} />
    ${d.cause && html`<div class="section-h"><h2>Who can explain the latest update</h2></div>
      <div class="card"><${PeopleList} people=${d.cause.people} onPerson=${openPerson} draft=${{ ticker, event_id: eventId }} />
        <div class="tiny muted" style="margin-top:8px">${d.cause.coverage.level === "strong" ? "Your network covers this: " : "Coverage is thin: "}${d.cause.coverage.reason}.</div>
        <${WebLeads} query=${d.cause.web_query} auto=${d.cause.coverage.search_web} /></div>`}
    <div class="section-h"><h2>Who knows ${short}</h2></div>
    <p class="tiny muted" style="margin:-4px 0 8px">People closest to ${short}${d.themes?.length ? `, its competitors, or ${d.themes.join(", ")}` : ""}.</p>
    <div class="card"><${PeopleList} people=${d.people} onPerson=${openPerson} draft=${{ ticker }} />
      ${!d.cause && !d.people?.length && html`<${WebLeads} query=${{ company: d.name, theme: d.themes?.[0], text: d.name }} auto=${true} />`}</div>
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
// Research: any ticker or question, plus the names you're watching
// ---------------------------------------------------------------------------
const SUGGESTIONS = ["How is Dixon's margin holding up, and why?", "What changed across my portfolio this week?", "Who can intro me to the new CFO of Deepak Nitrite?", "What did PI Industries guide for FY27?", "Who knows semiconductors?", "SRF"];

function ResearchView({ openAsk }) {
  const [q, setQ] = useState("");
  const [d, setD] = useState(null);
  const recent = useMemo(() => { try { return JSON.parse(localStorage.getItem("finn.recent") || "[]"); } catch { return []; } }, []);
  useEffect(() => { api("/api/portfolio").then(setD).catch(() => setD({ holdings: [], research: [] })); }, []);
  const names = Object.fromEntries((d ? [...d.holdings, ...d.research] : []).map((h) => [h.ticker, h]));
  const submit = (t) => { t = (t ?? q).trim(); if (!t) return; setQ(""); document.activeElement?.blur(); openAsk(t); };
  const Row = ({ t, h }) => html`<div class="list-row" onClick=${() => go(`/c/${t}`)}>
    <div style="min-width:0"><div class="pname">${h?.name || t}</div><div class="phead"><span class="tick">${t}</span> ${h?.theme || ""}</div></div>
    <div style="display:flex;align-items:center;gap:8px">${h?.metrics?.change_pct != null && html`<span class="delta ${tone(h.metrics.change_pct)}">${sgn(h.metrics.change_pct, 1)}</span>`}<span class="muted chev">${Icon.chev}</span></div></div>`;
  return html`<div>
    <div class="topbar"><div class="wordmark">Research</div></div>
    <div class="hero"><h1>What do you want to know?</h1><p>A ticker opens its dossier. A question goes to FINN, which reads filings, transcripts and your network and cites every figure.</p></div>
    <div class="section" style="padding-top:0">
      <form class="ask big" onSubmit=${(e) => { e.preventDefault(); submit(); }}>
        <span class="muted">${Icon.search}</span>
        <input value=${q} onInput=${(e) => setQ(e.target.value)} placeholder="Ticker or question" aria-label="Ticker or question" enterkeyhint="go" autocomplete="off" />
        <button class="go" type="submit" aria-label="Go">${Icon.arrow}</button>
      </form>
      <div class="section-h"><h2>Try asking</h2></div>
      <div class="suggest wrap" style="padding-top:0">${SUGGESTIONS.map((s) => html`<button onClick=${() => submit(s)}>${s}</button>`)}</div>
      ${recent.length > 0 && html`<div class="section-h"><h2>Recently opened</h2></div>
        <div class="card list">${recent.map((t) => html`<${Row} t=${t} h=${names[t]} />`)}</div>`}
      ${d?.research?.length > 0 && html`<div class="section-h"><h2>Your research list</h2><span class="tiny muted">not held</span></div>
        <div class="card list">${d.research.map((h) => html`<${Row} t=${h.ticker} h=${h} />`)}</div>`}
      <p class="tiny muted" style="text-align:center;margin-top:12px">Any NSE ticker works.</p>
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
// Flow 3: add people (keep-in-touch nudges now live in the People to-do list)
// ---------------------------------------------------------------------------
function AddContact({ onAdded, startOpen = false }) {
  const [open, setOpen] = useState(startOpen);
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

// ---------------------------------------------------------------------------
// People: who to contact now (top), the network at a glance, and everyone in it
// ---------------------------------------------------------------------------
function TodoRow({ t, onDone, lead }) {
  const ctx = { reason: t.reason, ticker: t.ticker, event_id: t.event?.id, target: t.target, intent: t.intent, todo: t.id };
  return html`<div class="todo">
    <div class="person" onClick=${() => ui.openPerson(t.person.id, ctx)}>
      <${Avatar} p=${t.person} cls=${t.kind === "news" ? "need" : t.kind === "lead" ? "target" : ""} />
      <div style="flex:1;min-width:0"><div class="pname">${t.title}</div>
        <div class="phead">${t.ticker && t.kind === "news" ? html`<span class="tick">${t.ticker}</span> ` : ""}${t.kind === "lead" ? "new lead" : html`<${Warmth} w=${t.warmth} />`}${t.simulated ? html` <span class="badge sim">Simulated</span>` : ""}</div></div>
    </div>
    <div class="small ink2 clamp2" style="margin-top:6px">${t.reason}</div>
    <div class="row-btns"><button class=${`btn ${lead ? "sig" : ""}`} onClick=${() => ui.openDraft({ person: t.person, ...ctx })}>${Icon.draft} Draft message</button>
      <button class="btn" onClick=${() => onDone(t)}>Done</button></div>
  </div>`;
}

function Todo({ d, setD }) {
  const [more, setMore] = useState(false);
  const [toast, setToast] = useState(null);
  useEffect(() => { if (!toast) return; const k = setTimeout(() => setToast(null), 4000); return () => clearTimeout(k); }, [toast]);
  if (!d) return html`<div class="skeleton"></div>`;
  if (!d.items.length) return html`<div class="empty"><b>No one needs a call right now</b><span>People land here when the news gives you a reason to call, or when you're overdue to catch up.</span></div>`;
  const done = async (t) => {
    setD((x) => ({ ...x, items: x.items.filter((y) => y.id !== t.id) }));
    haptic(15);
    setToast(`${first(t.person.name)}: done${t.person.is_contact ? ", keep-in-touch clock restarted" : ""}.`);
    await post("/api/people/todo/done", { id: t.id, person_id: t.person.is_contact ? t.person.id : null }).catch(() => {});
  };
  const N = 4, rows = more ? d.items : d.items.slice(0, N);
  return html`<div>
    <div class="card list">${rows.map((t, k) => html`<${TodoRow} key=${t.id} t=${t} lead=${k === 0} onDone=${done} />`)}</div>
    ${d.items.length > N && html`<button class="fold" style="width:100%;text-align:left" onClick=${() => setMore(!more)}>${more ? "Show fewer" : html`<b>${d.items.length - N}</b> more people worth a call`}</button>`}
    ${toast && html`<div class="toast" role="status"><span>${toast}</span></div>`}
  </div>`;
}

// Everyone you know, by how warm the relationship is: closer to the centre = spoke more recently.
function Orbit({ people, hot, onPerson }) {
  const [ref, W] = useWidth(343);
  const H = Math.round(Math.min(W, 380) * 0.88);
  const cx = W / 2, cy = H / 2, R = Math.min(W, H) / 2 - 18;
  const rings = { warm: R * 0.4, cooling: R * 0.69, dormant: R * 0.97 };
  const groups = { warm: [], cooling: [], dormant: [] };
  people.forEach((p) => (groups[p.warmth.label] || groups.dormant).push(p));
  const nodes = [];
  Object.entries(groups).forEach(([k, ps], gi) => ps.forEach((p, i) => {
    const a = -Math.PI / 2 + 0.45 + gi * 0.6 + (i / Math.max(1, ps.length)) * Math.PI * 2;
    nodes.push({ p, x: cx + Math.cos(a) * rings[k], y: cy + Math.sin(a) * rings[k] });
  }));
  return html`<div ref=${ref}><svg class="orbit" width=${W} height=${H} viewBox="0 0 ${W} ${H}" role="img" aria-label="Your contacts by how warm the relationship is">
    ${Object.entries(rings).map(([k, r]) => html`<circle cx=${cx} cy=${cy} r=${r} class="oring ${k}" />`)}
    <circle cx=${cx} cy=${cy} r="17" class="oyou" /><text x=${cx} y=${cy + 4} text-anchor="middle" class="oyout">You</text>
    ${nodes.map((n) => html`<g class="onode ${hot.has(n.p.id) ? "hot" : ""} ${n.p.warmth.label === "not yet contacted" ? "lead" : ""}" role="button" tabindex="0" aria-label=${n.p.name} onClick=${() => onPerson(n.p.id)}>
      <circle cx=${n.x} cy=${n.y} r="22" class="hit" /><circle cx=${n.x} cy=${n.y} r=${hot.has(n.p.id) ? 15 : 12.5} /><text x=${n.x} y=${n.y + 4} text-anchor="middle">${n.p.initials}</text></g>`)}
    ${Object.entries(rings).map(([k, r]) => html`<text x=${cx - r * 0.7071 - 4} y=${cy + r * 0.7071 + 14} text-anchor="end" class="olabel">${k}</text>`)}
  </svg></div>`;
}

const FILTERS = [["all", "All"], ["warm", "Warm"], ["cooling", "Cooling"], ["dormant", "Dormant"], ["lead", "New leads"]];
function Directory({ people, onPerson }) {
  const [q, setQ] = useState("");
  const [f, setF] = useState("all");
  const ql = q.trim().toLowerCase();
  const rows = people.filter((p) => (f === "all" || (f === "lead" ? p.warmth.label === "not yet contacted" : p.warmth.label === f))
    && (!ql || `${p.name} ${p.headline || ""} ${p.org || ""} ${(p.tags || []).join(" ")}`.toLowerCase().includes(ql)));
  return html`<div>
    <label class="searchbox">${Icon.search}<input value=${q} onInput=${(e) => setQ(e.target.value)} placeholder="Name, company or skill" aria-label="Search your network" autocomplete="off" /></label>
    <div class="chips-row" role="tablist" aria-label="Filter by warmth">${FILTERS.map(([k, l]) => html`<button role="tab" aria-selected=${f === k} class="toggle ${f === k ? "on" : ""}" onClick=${() => setF(k)}>${l}</button>`)}</div>
    <div class="card list">${rows.map((p) => html`<div class="list-row" onClick=${() => onPerson(p.id)}>
      <div class="person" style="min-width:0"><${Avatar} p=${p} /><div style="min-width:0"><div class="pname">${p.name}</div><div class="phead clamp1">${p.headline}</div></div></div>
      ${p.warmth.label === "not yet contacted" ? html`<span class="badge web">Lead</span>` : html`<${Warmth} w=${p.warmth} />`}</div>`)}
      ${!rows.length && html`<p class="small muted">No one matches.</p>`}</div>
  </div>`;
}

function PeopleView({ openPerson, openAsk }) {
  const [net, setNet] = useState(null);
  const [todo, setTodo] = useState(null);
  const [v, setV] = useState(0);
  useEffect(() => { api("/api/network").then(setNet).catch(() => setNet({ people: [], themes: [] })); }, [v]);
  useEffect(() => { api("/api/people/todo").then(setTodo).catch(() => setTodo({ items: [] })); }, [v]);
  const hot = new Set((todo?.items || []).slice(0, 4).map((t) => t.person.id));   // the four the list shows first
  const count = (l) => (net ? net.people.filter((p) => p.warmth.label === l).length : 0);
  return html`<div>
    <div class="topbar"><div class="wordmark">People <small>${net ? `${net.people.length} in your network` : ""}</small></div>
      <div style="display:flex;gap:2px"><button class="iconbtn" aria-label="Add someone to your network" onClick=${() => ui.openSheet({ kind: "addperson", onAdded: () => setV((x) => x + 1) })}>${Icon.plus}</button><${AccountButton} /></div></div>
    <div class="section" style="padding-top:0">
      <div class="section-h" style="margin-top:6px"><h2>To do</h2><span class="tiny muted">${todo?.items?.length ? `${todo.items.length} people` : ""}</span></div>
      <${Todo} d=${todo} setD=${setTodo} />
      <div class="section-h"><h2>Your network at a glance</h2><span class="tiny muted">${net ? `${count("warm")} warm · ${count("cooling")} cooling · ${count("dormant")} dormant` : ""}</span></div>
      <div class="card">${net ? html`<${Orbit} people=${net.people} hot=${hot} onPerson=${openPerson} />` : html`<div class="skeleton" style="height:260px;margin:0"></div>`}
        <div class="legend2" style="justify-content:center"><span><i class="lg-hot"></i>top of your to-do list</span><span><i class="lg-ring"></i>closer to you = spoke more recently</span></div></div>
      <div class="section-h"><h2>Everyone</h2></div>
      ${net ? html`<${Directory} people=${net.people} onPerson=${openPerson} />` : html`<div class="skeleton"></div>`}
      <div style="margin:12px 0 4px"><${AddContact} onAdded=${() => setV(v + 1)} /></div>
      ${net?.themes?.length > 0 && html`<div class="section-h"><h2>Who knows what</h2><span class="tiny muted">tap to ask</span></div>
        <div class="suggest wrap" style="padding-top:0">${net.themes.map((t) => html`<button onClick=${() => openAsk(`Who knows ${t.theme}?`)}>${t.theme} <span class="muted">${t.people.length}</span></button>`)}</div>`}
      <div style="margin-top:14px"><${Fold} title="How FINN ranks people" hint="the model"><${ModelCard} />
        <button class="btn" style="margin-top:10px" onClick=${() => ui.openSheet({ kind: "scoring" })}>${Icon.sliders} Edit scoring</button><//></div>
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
    <div class="upper muted">Alerts on this phone${ui.me && !ui.me.guest ? ` · for ${ui.me.name}` : ""}</div>
    ${ui.me?.guest && html`<p class="tiny muted" style="margin:4px 0 0">You're on the demo account. <button class="linkish tiny" onClick=${() => ui.openSheet({ kind: "account" })}>Sign in</button> so alerts follow your own radar.</p>`}
    ${!st.supported ? html`<p class="small ink2" style="margin:6px 0">${st.reason === "ios-install" ? "On iPhone, add FINN to your Home Screen first (Share, then Add to Home Screen). Then turn alerts on here." : "This browser can't receive push alerts."}</p>`
      : html`<div class="switchrow"><span class="small">Critical filings and your price/volume triggers, on the lock screen</span>
          <button class=${`switch ${st.on ? "on" : ""}`} role="switch" aria-checked=${st.on} aria-label="Phone alerts" disabled=${busy} onClick=${toggle}><i></i></button></div>
        ${st.on && html`<button class="linkish small" onClick=${test}>Send a test alert</button>`}`}
    ${msg && html`<p class="tiny ink2" style="margin:6px 0 0">${msg}</p>`}
  </div>`;
}

function PersonSheet({ id, ctx, openPerson }) {
  const [p, setP] = useState(null);
  const [touched, setTouched] = useState(false);
  useEffect(() => { setP(null); setTouched(false); api(`/api/people/${id}`).then(setP).catch(() => setP({ error: true })); }, [id]);
  if (!p) return html`<div class="loading">Loading…</div>`;
  if (p.error) return html`<div class="err">Couldn't load this person.</div>`;
  const f = first(p.name);
  const why = ctx?.reason || (p.nudge && (p.nudge.due || p.nudge.hook) ? p.nudge.reason : null);
  const touch = async () => { await post(`/api/network/contacts/${p.id}/touch`, {}).catch(() => {}); haptic(15); setTouched(true); };
  return html`<div>
    <div class="person" style="align-items:center">
      <${Avatar} p=${p} cls="lg ${p.is_contact ? "" : "target"}" />
      <div style="min-width:0"><h2 style="margin:0">${p.name}</h2><div class="phead">${p.headline}${p.location ? ` · ${p.location}` : ""}</div></div>
    </div>
    ${p.simulated && html`<p class="small" style="margin:10px 0 0"><span class="badge sim">Simulated profile</span> <span class="muted">${p.source}</span></p>`}
    <div class="row-btns" style="margin-top:14px">
      <button class="btn sig" onClick=${() => ui.openDraft({ person: p, intent: ctx?.intent, ticker: ctx?.ticker, event_id: ctx?.event_id, target: ctx?.target, todo: ctx?.todo })}>${Icon.draft} Draft a message</button>
      ${p.is_contact && html`<button class="btn" disabled=${touched} onClick=${touch}>${touched ? "Logged today" : "I spoke to them"}</button>`}
    </div>
    ${why && html`<div class="callout sigc"><span class="upper">Why get in touch now</span><div class="small">${why}</div></div>`}
    ${p.warmth && html`<div class="callout"><span class="upper">How you know ${f}</span>
      <div class="small ink2">${p.warmth.context}</div>
      <div style="margin-top:6px"><${Warmth} w=${p.warmth} />${p.warmth.last_contact ? html` <span class="tiny muted">· last spoke ${p.warmth.last_contact}</span>` : ""}</div>
      ${p.overlap_with_you?.length > 0 && html`<div class="small ink2" style="margin-top:6px">${p.overlap_with_you[0]}</div>`}
    </div>`}
    ${p.paths?.length > 0 && html`<div class="callout"><span class="upper">Your way in</span>
      ${p.paths.map((x, k) => k === 0 ? html`<${PathView} path=${x} target=${p} onPerson=${openPerson} /><${EvidenceGraph} g=${x.graph} />`
        : html`<div class="small ink2" style="margin-top:6px">${x.via ? html`Or via <b style="cursor:pointer" onClick=${() => openPerson(x.via.id)}>${x.via.name}</b>: ` : ""}${x.why[0]}</div>`)}
    </div>`}
    ${p.roles?.length > 0 && html`<div class="section-h"><h2>Career</h2></div>
      <${Timeline} roles=${p.roles} />
      <div style="margin-top:10px">${[...p.roles].reverse().map((r) => html`<div class="rline" style="grid-template-columns:1fr auto">
        <div><b>${r.title.split("→").pop().trim()}</b><div class="small muted">${r.org_name}${r.ticker ? html` · <span class="tick link" onClick=${() => go(`/c/${r.ticker}`)}>${r.ticker}</span>` : ""}</div></div>
        <span class="tiny muted num">${(r.from || "").slice(0, 4)}–${r.to ? r.to.slice(0, 4) : "now"}</span></div>`)}</div>`}
    ${p.education?.length > 0 && html`<div class="section-h"><h2>Education</h2></div>${p.education.map((e) => html`<div class="small ink2">${e.org_name}, ${e.degree} <span class="muted">(${e.from}–${e.to})</span></div>`)}`}
  </div>`;
}

// Message drafter: pick what you want and where it goes; FINN drafts from facts it can show you.
const CHANNELS = [["whatsapp", "WhatsApp"], ["email", "Email"], ["linkedin", "LinkedIn"]];
const INTENT_LABEL = { intro: "Ask for an intro", view: "Get their view", catchup: "Catch up", first: "Introduce yourself" };

function DraftSheet({ ctx, close }) {
  const person = ctx.person;
  const [intent, setIntent] = useState(ctx.intent || (ctx.target ? "intro" : "view"));
  const [channel, setChannel] = useState(() => lsGet("finn.channel") || "whatsapp");
  const [variant, setVariant] = useState(0);
  const [r, setR] = useState(null);
  const [text, setText] = useState("");
  const [subject, setSubject] = useState("");
  const [busy, setBusy] = useState(true);
  const [err, setErr] = useState(null);
  const [step, setStep] = useState(null);   // null → "copied" / "opened" → "logged"
  useEffect(() => {
    let live = true;
    setBusy(true); setErr(null);
    post("/api/draft", { person_id: person.id, intent, channel, ticker: ctx.ticker, event_id: ctx.event_id, target_id: ctx.target?.id, variant })
      .then((x) => { if (!live) return; setR(x); setText(x.text); setSubject(x.subject || ""); if (x.intent !== intent) setIntent(x.intent); })
      .catch((e) => live && setErr(e.message)).finally(() => live && setBusy(false));
    return () => { live = false; };
  }, [intent, channel, variant]);
  const pickChannel = (c) => { setChannel(c); lsSet("finn.channel", c); };
  const copy = async () => { try { await navigator.clipboard.writeText(channel === "email" && subject ? `${subject}\n\n${text}` : text); } catch {} haptic(15); setStep("copied"); };
  const openApp = () => setStep("opened");
  const logged = async () => { await post("/api/people/todo/done", { id: ctx.todo || "", person_id: person.is_contact !== false ? person.id : null }).catch(() => {}); haptic(15); setStep("logged"); };
  const intents = r?.intents || [{ id: intent, label: INTENT_LABEL[intent] }];
  const over = channel === "linkedin" && text.length > 300;
  return html`<div class="drafter">
    <div class="person" style="align-items:center"><${Avatar} p=${person} />
      <div style="min-width:0"><h2 style="margin:0;font-size:20px">Message to ${person.name}</h2>
        <div class="phead">${ctx.target ? `About an intro to ${ctx.target.name}` : ctx.ticker ? `About ${ctx.ticker}` : person.headline || ""}</div></div></div>
    <span class="field-l">What do you want?</span>
    <div class="seg full wrap2" role="radiogroup" aria-label="What you want">${intents.map((x) => html`<button role="radio" aria-checked=${x.id === intent} class=${x.id === intent ? "on" : ""} onClick=${() => setIntent(x.id)}>${x.label}</button>`)}</div>
    <span class="field-l">Send it on</span>
    <div class="seg full" role="radiogroup" aria-label="Channel">${CHANNELS.map(([k, l]) => html`<button role="radio" aria-checked=${k === channel} class=${k === channel ? "on" : ""} onClick=${() => pickChannel(k)}>${l}</button>`)}</div>
    <span class="field-l">Draft ${busy ? html`<span class="spin" style="width:11px;height:11px;vertical-align:-1px;margin-left:4px"></span>` : r ? html`<span class="muted">· ${r.source === "model" ? "written by the model from the facts below" : "from a template"}</span>` : ""}</span>
    ${err && html`<div class="err" style="margin:0 0 8px">${err}</div>`}
    ${channel === "email" && html`<input class="subj" value=${subject} onInput=${(e) => setSubject(e.target.value)} aria-label="Email subject" placeholder="Subject" />`}
    <textarea class="draftbox" value=${text} onInput=${(e) => setText(e.target.value)} aria-label="Message" rows="7" disabled=${busy && !text}></textarea>
    <div class="row tiny muted" style="margin-top:4px"><button class="linkish tiny" onClick=${() => setVariant(variant + 1)} disabled=${busy}>${Icon.refresh} Rewrite</button>
      <span class=${over ? "neg" : ""}>${channel === "linkedin" ? `${text.length}/300` : `${text.length} characters`}</span></div>
    ${r?.grounds?.length > 0 && html`<div class="callout" style="margin-top:10px"><span class="upper">Based on</span><ul class="grounds">${r.grounds.map((g) => html`<li>${g}</li>`)}</ul>
      ${r.simulated && html`<div class="tiny muted" style="margin-top:4px">The news here is a simulated demo scenario.</div>`}</div>`}
    <div class="row-btns sticky-actions">
      <button class="btn sig" onClick=${copy} disabled=${!text}>${Icon.copy} ${step === "copied" ? "Copied" : "Copy"}</button>
      ${channel === "whatsapp" && html`<a class="btn" href=${`https://wa.me/?text=${encodeURIComponent(text)}`} target="_blank" rel="noopener" onClick=${openApp}>Open WhatsApp</a>`}
      ${channel === "email" && html`<a class="btn" href=${`mailto:?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(text)}`} onClick=${openApp}>Open mail</a>`}
      ${channel === "linkedin" && html`<a class="btn" href="https://www.linkedin.com/messaging/" target="_blank" rel="noopener" onClick=${openApp}>Open LinkedIn</a>`}
    </div>
    ${step && step !== "logged" && html`<div class="callout"><div class="small">Sent it? Logging it restarts ${first(person.name)}'s keep-in-touch clock and clears the to-do.</div>
      <div class="row-btns"><button class="btn pri" onClick=${logged}>I sent it</button><button class="btn" onClick=${close}>Not yet</button></div></div>`}
    ${step === "logged" && html`<div class="callout sigc"><div class="small">Logged. ${first(person.name)} is off your to-do list.</div></div>`}
    <p class="tiny muted" style="margin-top:10px">FINN never sends anything for you: it copies the text or opens your own app.</p>
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

function RecRow({ x, onPerson, compact, draft }) {
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
    <div class="recfoot">
      ${x.contributions && html`<button class="linkish tiny whybtn" onClick=${() => setOpen(!open)}>${open ? "Hide" : `Why ${first(x.person.name)}? · ${Math.round(x.score * 100)}% match`}</button>`}
      ${draft && html`<button class="btn sm" onClick=${() => ui.openDraft({ person: x.person, intent: "view", ...draft })}>${Icon.draft} Draft</button>`}
    </div>
    ${open && html`<${WhyPerson} rec=${x} />`}
  </div>`;
}

// ---------------------------------------------------------------------------
// Ask FINN: the agent chat
// ---------------------------------------------------------------------------
// A citation names the document it points at ("May 2026 call p.15"), not a bare number.
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
function citeLabel(e) {
  if (!e) return "source";
  let c = (e.cite || e.title || "").replace(/^[A-Z][A-Z0-9&-]{1,19}\s+(?=\S)/, "");   // the ticker is already on screen
  c = c.replace(/^reported financials.*$/i, "Financials").replace(/^(quarterly|annual|balance_sheet|cash_flow|ratios) statements$/i, (_, s) => `${s.replace("_", " ")} figures`)
    .replace(/^results, (.+)$/i, "$1 results").replace(/^EOD (\d{4})-(\d{2})-(\d{2})$/, (_, y, m, d) => `Price, ${+d} ${MON[+m - 1]}`)
    .replace(/^exchange filing, (\d{4})-(\d{2})-(\d{2})$/, (_, y, m, d) => `Filing, ${+d} ${MON[+m - 1]}`)
    .replace(/, p\.(\d+)$/, " p.$1").replace(/^your network: (route via )?/, "").replace(/^profile: /, "").replace(/^web: /, "Web: ");
  if (/^https?:/.test(c)) c = host(c);
  c = c.charAt(0).toUpperCase() + c.slice(1);
  return c.length > 26 ? c.slice(0, 25) + "…" : c;
}
// Where a source came from, in words an investor uses.
function sourceKind(e) {
  const c = e.cite || "";
  if (e.source === "network") return "Your network";
  if (e.source === "secondary") return "Web";
  if (e.source === "market") return "Price";
  if (/ call, p\.|\bcall\b/i.test(c)) return "Earnings call";
  if (/annual report/i.test(c) || e.tool === "search_annual_report") return "Annual report";
  if (/results/i.test(c)) return "Results";
  if (/exchange filing/i.test(c)) return "Filing";
  return "Financials";
}

function md(text, labels = {}) {
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const inline = (s) => esc(s)
    .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<i>$2</i>").replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\[(E\d+(?:\s*,\s*E?\d+)*)\]/g, (_, ids) => {
      const seen = new Set();
      return ids.split(/\s*,\s*/).map((id) => (id.startsWith("E") ? id : "E" + id)).filter((k) => {
        const l = labels[k] || k; if (seen.has(l)) return false; seen.add(l); return true;   // one chip per document
      }).map((k) => `<button class="cite" data-e="${k}" aria-label="source: ${esc(labels[k] || k)}">${esc(labels[k] || k.slice(1))}</button>`).join("");
    });
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
const WORKING = { understand: "Working out what you're asking…", plan: "Deciding what to read…", execute: "Reading the filings, calls and numbers…",
  answer: "Writing it up…", verify: "Checking every figure against the sources…", remember: "Wrapping up…" };

// One quiet line ("Read 9 sources · checked 29 of 29 figures"); the machinery is one tap away, not in the answer.
function RunTrace({ r, open, toggle }) {
  const last = {}, count = {};
  r.nodes.forEach((n) => { last[n.node] = n; count[n.node] = (count[n.node] || 0) + 1; });
  const cur = r.nodes[r.nodes.length - 1];
  const n = r.evidence?.length || 0;
  const summary = r.done
    ? [n ? `Read ${n} source${n === 1 ? "" : "s"}` : "Nothing to read",
       r.verify?.figures ? `checked ${r.verify.supported} of ${r.verify.figures} figures` : null,
       r.ms && `${Math.max(1, Math.round(r.ms / 1000))}s`].filter(Boolean).join(" · ")
    : (cur && WORKING[cur.node]) || "Starting…";
  return html`<div class="run ${r.done ? "done" : ""}">
    <button class="run-head" onClick=${toggle} aria-expanded=${open}>
      <span class="run-line">${!r.done && html`<span class="spin"></span>`}<span>${summary}</span></span>
      <span class="tiny muted">${open ? "Hide" : "How I got this"}</span>
    </button>
    ${open && html`<div class="run-body">
      <span class="flow" style="margin:6px 0 8px">${FLOW.map(([k, l], i) => html`${i ? html`<span class="arrow">›</span>` : ""}<span class="fnode ${last[k]?.status || "idle"}" title=${last[k]?.detail || ""}>${l}${count[k] > 1 ? html`<sup>×${count[k]}</sup>` : ""}</span>`)}</span>
      ${r.nodes.map((x) => html`<div class="rn"><span class="rdot ${x.status}"></span><b>${x.label}</b> <span class="muted">${x.detail || (x.status === "running" ? "…" : "")}</span>${x.ms != null ? html` <span class="tiny muted num">${x.ms} ms</span>` : ""}</div>`)}
      ${r.plan?.length > 0 && html`<div class="steps"><div class="tiny muted upper" style="margin-bottom:4px">What I read · ${r.planSource === "model" ? "planned by the model" : "rule planner"}</div>${r.plan.map((st) => {
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
    <div class="meta"><span class="src ${e.source}">${sourceKind(e)}</span><span>${e.cite}</span>
      ${e.url && html`<a class="linkish" href=${e.url} target="_blank" rel="noopener">open ↗</a>`}
      <button class="linkish" style="margin-left:auto" onClick=${onClose}>close</button></div>
    <div class="evtitle">${e.title}</div>
    <div class="evtext">${e.text}</div>
    ${e.graph && html`<${EvidenceGraph} g=${e.graph} />`}
    ${e.person && html`<button class="pbtn ghost" style="margin-top:6px" onClick=${() => openPerson(e.person.id)}>Open ${e.person.name}</button>`}
  </div>`;
}

// "ETERNAL filing: Shareholders Letter" → "Shareholders Letter"; the ticker is already on screen.
const noTicker = (s) => { s = (s || "").replace(/^[A-Z][A-Z0-9&-]{1,19}\s+(?:filing:\s*)?/, ""); return s.charAt(0).toUpperCase() + s.slice(1); };

function SourceRow({ e, onOpen, open }) {
  return html`<div class="srow ${open ? "on" : ""}" role="button" tabindex="0" onClick=${() => onOpen(e.id)} onKeyDown=${(k) => k.key === "Enter" && onOpen(e.id)}>
    <span class="skind">${sourceKind(e)}</span>
    <span class="sname">${sourceKind(e) === "Filing" ? `${noTicker(e.title)} · ${citeLabel(e).replace(/^Filing, /, "")}` : noTicker(e.cite || e.title)}</span>
    ${e.url ? html`<a class="sopen" href=${e.url} target="_blank" rel="noopener" aria-label=${`Open ${e.cite || "source"}`} onClick=${(x) => x.stopPropagation()}>↗</a>` : html`<span></span>`}
  </div>`;
}

function Turn({ r, onFollow, openPerson }) {
  const [openEv, setOpenEv] = useState(null);
  const [trace, setTrace] = useState(false);
  const [more, setMore] = useState(false);
  const evs = r.evidence || [];
  const byId = Object.fromEntries(evs.map((e) => [e.id, e]));
  const labels = Object.fromEntries(evs.map((e) => [e.id, citeLabel(e)]));
  const ev = byId[openEv];
  // The sources the answer actually cites, in the order it cites them; the rest were read but not used.
  const cited = r.answer ? [...new Set([...r.answer.text.matchAll(/\[(E\d+(?:\s*,\s*E?\d+)*)\]/g)]
    .flatMap((m) => m[1].split(/\s*,\s*/).map((id) => (id.startsWith("E") ? id : "E" + id))))].filter((id) => byId[id]) : [];
  const unused = evs.filter((e) => !cited.includes(e.id));
  const people = evs.filter((x) => x.source === "network" && x.graph);
  const toggleEv = (id) => setOpenEv(id === openEv ? null : id);
  return html`<div class="turn">
    <div class="bubble">${r.q}</div>
    <${RunTrace} r=${r} open=${trace} toggle=${() => setTrace(!trace)} />
    ${r.answer && html`<div class="answer">
      <div class="md" onClick=${(e) => { const b = e.target.closest(".cite"); if (b) toggleEv(b.dataset.e); }}
        dangerouslySetInnerHTML=${{ __html: md(r.answer.text, labels) }}></div>
      ${ev && html`<${EvidenceCard} e=${ev} onClose=${() => setOpenEv(null)} openPerson=${openPerson} />`}
      ${people.length > 0 && html`<div class="people-strip">${people.slice(0, 3).map((p) => html`<button class="pchip" onClick=${() => toggleEv(p.id)}><${Avatar} p=${p.person || { name: p.title.replace(/^.*?: /, "") }} /><span>${(p.person?.name || p.title).split(" ")[0]}</span></button>`)}<span class="tiny muted">tap to see why</span></div>`}
      ${evs.length > 0 && html`<div class="srcbox">
        <div class="srcbox-h"><span class="upper">${cited.length ? "Sources I used" : "What I read"}</span>
          ${r.verify && html`<span class=${`vbadge ${r.verify.ok ? "ok" : "warn"}`} title=${r.verify.ok ? "" : [...r.verify.unsupported, ...r.verify.uncited].join(" · ")}>
            ${r.verify.ok ? "✓" : "!"} ${r.verify.figures ? `${r.verify.supported} of ${r.verify.figures} figures checked` : "grounded in these sources"}${r.verify.ok ? "" : ` · ${r.verify.unsupported.length + r.verify.uncited.length} to check`}</span>`}</div>
        ${(cited.length ? cited.map((id) => byId[id]) : evs).map((e) => html`<${SourceRow} key=${e.id} e=${e} open=${openEv === e.id} onOpen=${toggleEv} />`)}
        ${cited.length > 0 && unused.length > 0 && html`<button class="linkish small" onClick=${() => setMore(!more)}>${more ? "Hide the rest" : `Also read ${unused.length} more`}</button>
          ${more && unused.map((e) => html`<${SourceRow} key=${e.id} e=${e} open=${openEv === e.id} onOpen=${toggleEv} />`)}`}
        ${r.answer.confidence && r.answer.confidence !== "high" && html`<div class="tiny muted" style="margin-top:6px">I'm ${r.answer.confidence === "low" ? "not confident" : "only fairly confident"} in this: the sources are thin.</div>`}
      </div>`}
      ${r.answer.follow_ups?.length > 0 && r.done && html`<div class="section-h" style="margin-top:14px"><h2>Ask next</h2></div><div class="suggest wrap" style="padding-top:0">${r.answer.follow_ups.map((f) => html`<button onClick=${() => onFollow(f)}>${f}</button>`)}</div>`}
      ${r.learned?.length > 0 && html`<div class="tiny muted" style="margin-top:6px">I'll remember: ${r.learned.join("; ")}</div>`}
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
      <div class="hero" style="padding:4px 0 10px"><h1 style="font-size:26px">Ask me anything about your companies.</h1>
        <p>I read the filings, earnings calls, financials and your network${info?.tools?.some((x) => x.name === "web_search") ? ", and the web when it helps" : ""}, then answer with every figure tied to the document it came from.</p></div>
      ${info && html`<div class="card"><span class="upper muted">How I answer</span><${AgentGraph} g=${info.graph} /></div>`}
      <div class="suggest wrap">${sugg.map((s) => html`<button onClick=${() => send(s)}>${s}</button>`)}</div>
      ${info?.facts?.length > 0 && html`<div class="card"><span class="upper muted">What I remember about you</span>
        ${info.facts.map((f) => html`<div class="rline" style="grid-template-columns:1fr auto"><span class="small">${f.text}</span>
          <button class="linkish tiny" onClick=${async () => setInfo({ ...info, ...(await api(`/api/agent/memory/${f.id}`, { method: "DELETE" })) })}>forget</button></div>`)}</div>`}
      ${info && !info.model.enabled && html`<p class="tiny muted">No model key configured: I plan with rules and quote the sources instead of writing them up.</p>`}
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
// Accounts: sign in, create an account, sign out. Alerts follow the account on this device.
// ---------------------------------------------------------------------------
function AccountButton() {
  const me = ui.me;
  return html`<button class="iconbtn acct" aria-label=${me && !me.guest ? `Account: ${me.name}` : "Sign in"} onClick=${() => ui.openSheet({ kind: "account" })}>
    ${me && !me.guest ? html`<span class="acct-i">${me.name.split(" ").map((w) => w[0]).slice(0, 2).join("").toUpperCase()}</span>` : Icon.user}</button>`;
}

// Switching accounts on one device: the push registration belongs to an account, and cached API
// responses belong to whoever fetched them, so both move with the sign-in.
async function switchAccount(doIt) {
  let wasOn = false;
  try { const st = await pushState(); wasOn = !!st.on; if (wasOn) await disablePush(); } catch {}
  const r = await doIt();
  try {
    for (const k of await caches.keys()) {
      const c = await caches.open(k);
      for (const req of await c.keys()) if (new URL(req.url).pathname.startsWith("/api/")) await c.delete(req);
    }
  } catch {}
  radarCache = null;
  if (wasOn) { try { await enablePush(); } catch {} }
  location.hash = "#/";
  location.reload();
  return r;
}

function AccountSheet({ openScoring }) {
  const me = ui.me;
  const [mode, setMode] = useState("signin");
  const [f, setF] = useState({ username: "", password: "", name: "", start_from_demo: true });
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const set = (k) => (e) => setF({ ...f, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value });
  const submit = async (e) => {
    e.preventDefault(); setErr(""); setBusy(true);
    try { await switchAccount(() => post(mode === "signin" ? "/api/auth/login" : "/api/auth/register", f)); }
    catch (x) { setErr(x.message); setBusy(false); }
  };
  if (me && !me.guest) return html`<div>
    <div class="person" style="align-items:center"><div class="avatar lg">${me.name.split(" ").map((w) => w[0]).slice(0, 2).join("").toUpperCase()}</div>
      <div><h2 style="margin:0">${me.name}</h2><div class="phead">Signed in as ${me.user}</div></div></div>
    <${PhoneAlerts} />
    <${KeyRow} />
    <div class="row-btns" style="margin-top:12px"><button class="btn" onClick=${openScoring}>${Icon.sliders} Scoring</button>
      <button class="btn" onClick=${() => switchAccount(() => post("/api/auth/logout", {}))}>Sign out</button></div>
    <p class="tiny muted" style="margin-top:12px">Your radar, network, scoring and alerts are yours alone: the database only ever shows this account its own rows.</p>
  </div>`;
  return html`<div>
    <h2>${mode === "signin" ? "Sign in" : "Create an account"}</h2>
    <p class="small muted" style="margin-top:0">You're looking at the demo investor. Sign in to keep your own radar and network, and to get alerts on this phone.</p>
    <form class="addform" onSubmit=${submit}>
      <label>Username<input required autocomplete="username" autocapitalize="none" spellcheck="false" value=${f.username} onInput=${set("username")} /></label>
      ${mode === "register" && html`<label>Your name<input autocomplete="name" value=${f.name} onInput=${set("name")} /></label>`}
      <label>Password<input required type="password" minlength=${mode === "register" ? 8 : 1} autocomplete=${mode === "signin" ? "current-password" : "new-password"} value=${f.password} onInput=${set("password")} /></label>
      ${mode === "register" && html`<label class="check"><input type="checkbox" checked=${f.start_from_demo} onChange=${set("start_from_demo")} />Start from the demo portfolio and network (you can change everything)</label>`}
      ${err && html`<div class="err" style="margin:0">${err}</div>`}
      <button class="pbtn" type="submit" disabled=${busy}>${busy ? "One moment…" : mode === "signin" ? "Sign in" : "Create account"}</button>
    </form>
    <button class="linkish small tap" onClick=${() => { setMode(mode === "signin" ? "register" : "signin"); setErr(""); }}>${mode === "signin" ? "New here? Create an account" : "Have an account? Sign in"}</button>
    <${KeyRow} />
  </div>`;
}

function KeyRow() {
  const c = modelKey.cfg();
  return html`<div class="keyrow"><div style="min-width:0"><div class="t">AI model</div><div class="s">${c ? modelKey.label(c) : "Not set: FINN runs without your own model"}</div></div>
    <button class="btn sm" onClick=${() => ui.openSheet({ kind: "modelkey" })}>${c ? "Change" : "Connect"}</button></div>`;
}

// Pick any provider, paste a key, choose models from the ones that key can actually use.
function ModelKeySheet({ close, first }) {
  const cur = modelKey.cfg();
  const [info, setInfo] = useState(null);
  const [prov, setProv] = useState(cur?.provider || "gemini");
  const [k, setK] = useState("");
  const [base, setBase] = useState(cur?.base || "");
  const [chk, setChk] = useState(null);
  const [fast, setFast] = useState("");
  const [deep, setDeep] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => { api("/api/llm").then(setInfo).catch(() => {}); }, []);
  const p = info?.providers?.find((x) => x.id === prov);
  const check = async (e) => {
    e.preventDefault(); setBusy(true); setChk(null);
    try {
      const r = await post("/api/llm/check", { key: k.trim(), provider: prov, base: base.trim() });
      setChk(r);
      if (r.ok) { setFast(r.fast || r.models[0] || ""); setDeep(r.deep || r.fast || r.models[0] || ""); haptic(8); }
    } catch (x) { setChk({ ok: false, error: x.message }); }
    setBusy(false);
  };
  const save = () => {
    modelKey.set({ provider: prov, label: chk.label, key: k.trim(), fast, deep, base: prov === "custom" ? base.trim() : "" });
    haptic(12); close(); location.reload();
  };
  const remove = () => { modelKey.clear(); modelKey.skip(); close(); location.reload(); };
  const pickModel = (label, v, set, hint) => html`<label>${label}<span class="tiny muted" style="font-weight:400"> ${hint}</span>
    <select value=${v} onChange=${(e) => set(e.target.value)}>${chk.models.map((m) => html`<option value=${m}>${m}</option>`)}</select></label>`;
  return html`<div>
    <h2>${first ? "Connect your AI model" : "Your AI model"}</h2>
    <p class="small muted" style="margin-top:0">FINN works with any major model. Pick a provider, paste your API key, and choose models. The key stays on this device and goes only to FINN, with your own requests. FINN never saves it.</p>
    ${cur && html`<div class="keynow"><span>✓ ${modelKey.label(cur)}</span><button class="linkish tiny tap" onClick=${remove}>Remove</button></div>`}
    ${!chk?.ok ? html`<form class="addform" onSubmit=${check}>
      <label>Provider<select value=${prov} onChange=${(e) => { setProv(e.target.value); setChk(null); }}>
        ${(info?.providers || [{ id: "gemini", label: "Google Gemini" }]).map((x) => html`<option value=${x.id}>${x.label}</option>`)}</select></label>
      ${p?.custom && html`<label>API base URL<input required type="url" inputmode="url" placeholder="https://api.example.com/v1" value=${base} onInput=${(e) => setBase(e.target.value)} /></label>`}
      <label>API key<input required type="password" autocomplete="off" autocapitalize="none" spellcheck="false" placeholder=${p?.hint || "your key"} value=${k} onInput=${(e) => setK(e.target.value)} /></label>
      ${p && !p.search && html`<div class="tiny muted">Web lookups (news context, finding people online) need Anthropic or Gemini; with ${p.label} everything else runs on your models.</div>`}
      ${chk && !chk.ok && html`<div class="err" style="margin:0">${chk.error}</div>`}
      <button class="pbtn" type="submit" disabled=${busy || !k.trim()}>${busy ? "Checking…" : "Check key"}</button>
    </form>` : html`<div class="addform">
      <div class="okmsg">${chk.label} key works · ${chk.models.length} models available</div>
      ${pickModel("Everyday model", fast, setFast, "one-liners, extraction, routing")}
      ${pickModel("Deep model", deep, setDeep, "judgement and answers")}
      <div class="row-btns"><button class="btn sig" onClick=${save}>Use these models</button><button class="btn" onClick=${() => setChk(null)}>Back</button></div>
    </div>`}
    ${!cur && !chk?.ok && html`<button class="linkish small tap" onClick=${() => { modelKey.skip(); close(); }}>${info?.server_key ? "Skip: use FINN's shared model" : "Skip: use FINN without AI for now"}</button>`}
    <p class="tiny muted" style="margin-top:10px">Without a model FINN still works: filings are sorted by rules, numbers come straight from the source, and answers are assembled from the evidence.</p>
  </div>`;
}

// ---------------------------------------------------------------------------
// The radar: add a company, remove one
// ---------------------------------------------------------------------------
function AddCompanySheet({ close, preset }) {
  const [q, setQ] = useState("");
  const [res, setRes] = useState([]);
  const [pick, setPick] = useState(preset || null);
  const [kind, setKind] = useState("portfolio");
  const [w, setW] = useState("2");
  const [msg, setMsg] = useState("");
  useEffect(() => {
    if (q.trim().length < 2) { setRes([]); return; }
    const k = setTimeout(() => api(`/api/universe/search?q=${encodeURIComponent(q.trim())}`).then((r) => setRes(r.results)).catch(() => setRes([])), 180);
    return () => clearTimeout(k);
  }, [q]);
  const add = async () => {
    setMsg("");
    try {
      const r = await post("/api/holdings", { ticker: pick.ticker, kind, weight: kind === "portfolio" ? w : null });
      haptic(15); radarCache = null; ui.radarChanged();
      setMsg(`${r.name} ${kind === "portfolio" ? "is on your radar" : "is on your research list"}. Its filings and prices load in the background.`);
      setTimeout(() => { close(); go(kind === "portfolio" ? "/" : "/research"); }, 1100);
    } catch (e) { setMsg(e.message); }
  };
  return html`<div>
    <h2>Add a company</h2>
    ${!pick ? html`<div>
      <label class="searchbox">${Icon.search}<input value=${q} onInput=${(e) => setQ(e.target.value)} placeholder="Ticker or company name" aria-label="Search NSE companies" autocomplete="off" autocapitalize="characters" /></label>
      <div class="card list">${res.map((r) => html`<div class="list-row" onClick=${() => setPick(r)}>
        <div style="min-width:0"><div class="pname clamp1">${r.name}</div><div class="phead"><span class="tick">${r.ticker}</span>${r.on ? ` · ${r.on === "portfolio" ? "on your radar" : "on your research list"}` : ""}</div></div>
        <span class="muted chev">${Icon.plus}</span></div>`)}
        ${q.trim().length >= 2 && !res.length && html`<p class="small muted">No NSE-listed company matches.</p>`}
        ${q.trim().length < 2 && html`<p class="small muted">Any NSE-listed company. Type at least two letters.</p>`}</div></div>`
    : html`<div class="card">
      <div class="pname">${pick.name}</div><div class="phead"><span class="tick">${pick.ticker}</span></div>
      <div class="seg" role="tablist" style="margin:12px 0" aria-label="Where to add it">${[["portfolio", "I hold it"], ["research", "Watch only"]].map(([k, l]) =>
        html`<button role="tab" aria-selected=${kind === k} class=${kind === k ? "on" : ""} onClick=${() => setKind(k)}>${l}</button>`)}</div>
      ${kind === "portfolio" ? html`<label class="addform" style="display:grid"><span class="tiny muted">Weight in your portfolio</span>
          <div class="row" style="gap:8px"><input type="number" inputmode="decimal" min="0.1" max="100" step="0.1" value=${w} onInput=${(e) => setW(e.target.value)} style="max-width:110px" /><span class="small">%</span></div></label>
          <p class="tiny muted">Sizes its tile on the radar and how much its filings count.</p>`
        : html`<p class="tiny muted">On your research list: its dossier and people, without radar alerts.</p>`}
      <div class="row-btns"><button class="btn sig" onClick=${add}>${kind === "portfolio" ? "Add to radar" : "Add to research list"}</button>${!preset && html`<button class="btn" onClick=${() => setPick(null)}>Back</button>`}</div>
    </div>`}
    ${msg && html`<p class="small ink2" role="status">${msg}</p>`}
  </div>`;
}

function RadarMembership({ ticker, name, held }) {
  const [sure, setSure] = useState(false);
  const [gone, setGone] = useState(false);
  if (gone) return html`<span class="tiny muted">Removed</span>`;
  if (!held) return html`<button class="linkish tiny tap" onClick=${() => ui.openSheet({ kind: "addco", preset: { ticker, name } })}>${Icon.plus} Add to radar</button>`;
  const remove = async () => {
    if (!sure) { setSure(true); setTimeout(() => setSure(false), 3000); return; }
    await api(`/api/holdings/${ticker}`, { method: "DELETE" }); haptic(15); radarCache = null; ui.radarChanged(); setGone(true);
  };
  return html`<button class="linkish tiny tap" onClick=${remove}>${sure ? "Tap again to remove" : "Remove from radar"}</button>`;
}

// ---------------------------------------------------------------------------
// Scoring: FINN's defaults, editable per investor
// ---------------------------------------------------------------------------
const NOT_SHARE = new Set(["min_priority", "push_priority", "min_fit", "reach_floor", "fresh_floor"]);

function ScoringSheet() {
  const [g, setG] = useState(null);
  const [vals, setVals] = useState({});
  const [msg, setMsg] = useState({});
  const load = (groups) => { setG(groups); setVals(Object.fromEntries(groups.map((x) => [x.id, Object.fromEntries(x.fields.map((f) => [f.key, f.value]))]))); };
  useEffect(() => { api("/api/scoring").then((r) => load(r.groups)).catch(() => setG([])); }, []);
  if (!g) return html`<div class="loading">Loading…</div>`;
  const put = (gid, k, v) => setVals({ ...vals, [gid]: { ...vals[gid], [k]: +v } });
  const done = (gid, r, text) => { setG(g.map((x) => (x.id === gid ? r : x))); setVals({ ...vals, [gid]: Object.fromEntries(r.fields.map((f) => [f.key, f.value])) }); setMsg({ ...msg, [gid]: text }); haptic(10); };
  const save = async (gid) => done(gid, await api(`/api/scoring/${gid}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ values: vals[gid] }) }),
    gid === "pull" || gid === "push" ? "Saved. This is now your starting point (it replaces what FINN had learned from your taps)." : "Saved. Rankings use your weights now.");
  const reset = async (gid) => done(gid, await api(`/api/scoring/${gid}`, { method: "DELETE" }), "Back to FINN's defaults.");
  return html`<div>
    <h2>Scoring</h2>
    <p class="small muted" style="margin-top:0">Every ranking starts from FINN's defaults. Move what matters to you; the tick marks the default.</p>
    ${g.map((grp) => {
      const v = vals[grp.id];
      const shares = grp.fields.filter((f) => grp.kind === "shares" && !NOT_SHARE.has(f.key));
      const tot = shares.reduce((s, f) => s + (v[f.key] || 0), 0) || 1;
      return html`<${Fold} title=${grp.label} hint=${grp.customised ? "yours" : "default"}>
        <p class="tiny muted" style="margin-top:0">${grp.about}</p>
        ${grp.fields.map((f) => {
          const isShare = grp.kind === "shares" && !NOT_SHARE.has(f.key);
          const pct = (x) => ((x - f.min) / (f.max - f.min)) * 100;
          return html`<div class="sfield">
            <div class="row"><span class="small">${f.label}</span><b class="num small">${isShare ? `${Math.round((v[f.key] / tot) * 100)}%` : (+v[f.key]).toFixed(f.step < 0.1 ? 2 : 1)}</b></div>
            <div class="srange"><input type="range" min=${f.min} max=${f.max} step=${f.step} value=${v[f.key]} aria-label=${f.label} onInput=${(e) => put(grp.id, f.key, e.target.value)} />
              <u style=${`left:${pct(f.default)}%`} title=${`default ${f.default}`}></u></div>
            <div class="tiny muted">${f.help}${Math.abs(v[f.key] - f.default) > 1e-9 ? ` · default ${f.default}` : ""}</div>
          </div>`;
        })}
        <div class="row-btns"><button class="btn sig" onClick=${() => save(grp.id)}>Save</button><button class="btn" onClick=${() => reset(grp.id)}>Reset to default</button></div>
        ${msg[grp.id] && html`<p class="tiny ink2" role="status">${msg[grp.id]}</p>`}
      <//>`;
    })}
    <p class="tiny muted">How each score works: docs/SCORING.md in the repository.</p>
  </div>`;
}

// ---------------------------------------------------------------------------
// Notifications: the bell, and an inbox of everything FINN alerted you about
// ---------------------------------------------------------------------------
function Bell() {
  const n = ui.unread;
  return html`<button class="iconbtn bell" aria-label=${n ? `Notifications, ${n} unread` : "Notifications"} onClick=${() => { haptic(6); ui.openSheet({ kind: "inbox" }); }}>
    ${Icon.bell}${n > 0 && html`<span class="dot-n">${n > 9 ? "9+" : n}</span>`}</button>`;
}

const DAY = 864e5;
function InboxSheet({ close }) {
  const [d, setD] = useState(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const load = () => api("/api/notifications").then(setD).catch(() => setD({ items: [], unread: 0 }));
  useEffect(() => { load(); }, []);
  const open = async (it) => {
    if (!it.read) { await post("/api/notifications/read", { ids: [it.id] }).catch(() => {}); ui.inboxChanged(); }
    close(); if (it.url) location.hash = it.url.replace(/^\/#/, "");
  };
  const readAll = async () => { await post("/api/notifications/read", {}); haptic(10); ui.inboxChanged(); load(); };
  const check = async () => {
    setBusy(true); setMsg("");
    try { const r = await post("/api/notifications/check", {}); setMsg(r.new ? `${r.new} new` : "Nothing new since the last check."); ui.inboxChanged(); load(); }
    catch (e) { setMsg(e.message); } finally { setBusy(false); }
  };
  if (!d) return html`<div class="loading">Loading…</div>`;
  const now = Date.now(), groups = [["Today", (it) => now - it.at * 1000 < DAY], ["This week", (it) => now - it.at * 1000 < 7 * DAY], ["Earlier", () => true]];
  const seen = new Set();
  return html`<div>
    <div class="row" style="display:flex;justify-content:space-between;align-items:center"><h2 style="margin:0">Notifications</h2>
      ${d.unread > 0 && html`<button class="linkish small tap" onClick=${readAll}>Mark all read</button>`}</div>
    <p class="small muted" style="margin:4px 0 10px">Critical filings, your price and volume triggers, and people worth calling.${d.devices ? ` Also pushed to ${d.devices} device${d.devices > 1 ? "s" : ""}.` : " Turn on alerts below to get them on your lock screen too."}</p>
    <div class="row-btns" style="margin-bottom:6px"><button class="btn" disabled=${busy} onClick=${check}>${busy ? "Checking…" : "Check for news now"}</button>${msg && html`<span class="tiny ink2" role="status">${msg}</span>`}</div>
    ${!d.items.length && html`<div class="empty-hero" style="margin-top:12px"><div class="ring">${Icon.bell}</div><b>Nothing yet</b>
      <p class="small" style="margin:6px 0 0">When something on your radar needs you, it lands here first.</p></div>`}
    ${groups.map(([label, f]) => { const rows = d.items.filter((it) => !seen.has(it.id) && f(it)); rows.forEach((it) => seen.add(it.id));
      return rows.length > 0 && html`<div><div class="ibx-group">${label}</div>${rows.map((it) => html`<div class=${`ibx-row ${it.read ? "" : "unread"}`} role="link" tabindex="0" onClick=${() => open(it)}>
        <i class="k"></i><div><div class="t">${it.title}</div><div class="b">${it.body}</div><div class="tiny muted">${ago(new Date(it.at * 1000).toISOString())}</div></div></div>`)}</div>`; })}
    <div style="margin-top:14px"><${PhoneAlerts} /></div>
  </div>`;
}

// ---------------------------------------------------------------------------
// Tabs + App
// ---------------------------------------------------------------------------
const TABS = [["/", "radar", "Radar"], ["/research", "research", "Research"], ["/people", "people", "People"]];

function TabBar({ tab }) {
  return html`<div class="dock"><div class="dock-inner"><nav class="tabs" aria-label="Sections">${TABS.map(([p, k, l]) =>
    html`<button class=${tab === k ? "on" : ""} aria-current=${tab === k ? "page" : null} onClick=${() => { if (tab !== k) haptic(6); go(p); }}>${Icon[k]}<span>${l}</span></button>`)}</nav></div></div>`;
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
  const [me, setMe] = useState(null);
  useEffect(() => { api("/api/auth/me").then(setMe).catch(() => setMe({ user: "demo", name: "Demo investor", guest: true })); }, []);
  useEffect(() => { if (!modelKey.get() && !modelKey.skipped()) setSheet({ kind: "modelkey", first: true }); }, []);
  const [unread, setUnread] = useState(0);
  const pollInbox = () => api("/api/notifications?limit=1").then((r) => setUnread(r.unread)).catch(() => {});
  useEffect(() => { pollInbox(); const k = setInterval(pollInbox, 60000); return () => clearInterval(k); }, []);
  ui.unread = unread;
  ui.inboxChanged = pollInbox;
  const openPerson = (id, ctx) => setSheet({ kind: "person", id, ctx });
  const openDraft = (ctx) => setSheet({ kind: "draft", ctx });
  ui.openPerson = openPerson;
  ui.openDraft = openDraft;
  ui.openSheet = setSheet;
  ui.me = me;
  ui.radarChanged = () => setRulesVersion((v) => v + 1);
  // One word (a ticker or a name) is a lookup; anything longer is a question for the agent.
  const openAsk = (q) => (/^\s*[A-Za-z0-9&.\-]{2,20}\s*$/.test(q) ? setSheet({ kind: "ask", q }) : go(`/chat?q=${encodeURIComponent(q)}`));
  const close = () => setSheet(null);
  useEffect(() => {   // old routes and push-notification deep links
    if (route.startsWith("/portfolio")) go("/");
    else if (route.startsWith("/network")) go("/people");
    const pm = route.match(/^\/p\/([\w:.-]+)/);
    if (pm) { go("/people"); openPerson(pm[1]); }
  }, [route]);
  const m = route.match(/^\/c\/([A-Za-z0-9&\-]+)/);
  const chat = route.startsWith("/chat");
  const held = m && (!radarCache || radarCache.tiles.some((t) => t.ticker === m[1].toUpperCase()));
  const tab = route.startsWith("/research") ? "research" : route.startsWith("/people") || route.startsWith("/p/") ? "people" : m ? (held ? "radar" : "research") : "radar";
  useEffect(() => { if (!m && !chat) window.scrollTo(0, 0); }, [route.split("?")[0]]);   // a tab opens at its top
  return html`<div class="app">
    <div class="view-in" key=${route.split("?")[0]}>
    ${chat ? html`<${ChatView} key=${route} params=${Object.fromEntries(new URLSearchParams(route.split("?")[1] || ""))} openPerson=${openPerson} />`
      : m ? html`<${HoldingView} key=${m[1]} ticker=${m[1].toUpperCase()} openPerson=${openPerson} />`
      : tab === "research" ? html`<${ResearchView} openAsk=${openAsk} />`
      : tab === "people" ? html`<${PeopleView} openPerson=${openPerson} openAsk=${openAsk} />`
      : html`<${RadarView} openRules=${() => setSheet({ kind: "rules" })} rulesVersion=${rulesVersion} />`}
    </div>
    ${!chat && html`<${TabBar} tab=${tab} />`}
    ${sheet && html`<${Sheet} key=${sheet.kind + (sheet.id || "")} onClose=${close}>
      ${sheet.kind === "person" && html`<${PersonSheet} id=${sheet.id} ctx=${sheet.ctx} openPerson=${openPerson} />`}
      ${sheet.kind === "draft" && html`<${DraftSheet} ctx=${sheet.ctx} close=${close} />`}
      ${sheet.kind === "ask" && html`<${AskSheet} q=${sheet.q} openPerson=${openPerson} close=${close} />`}
      ${sheet.kind === "rules" && html`<${RulesSheet} onSaved=${() => { close(); setRulesVersion((v) => v + 1); }} />`}
      ${sheet.kind === "account" && html`<${AccountSheet} openScoring=${() => setSheet({ kind: "scoring" })} />`}
      ${sheet.kind === "scoring" && html`<${ScoringSheet} />`}
      ${sheet.kind === "modelkey" && html`<${ModelKeySheet} close=${close} first=${sheet.first} />`}
      ${sheet.kind === "inbox" && html`<${InboxSheet} close=${close} />`}
      ${sheet.kind === "addco" && html`<${AddCompanySheet} close=${close} preset=${sheet.preset} />`}
      ${sheet.kind === "addperson" && html`<div><h2>Add someone</h2><p class="small muted" style="margin-top:0">FINN uses their career to rank them for events, and reminds you to keep in touch.</p>
        <${AddContact} startOpen=${true} onAdded=${() => { sheet.onAdded?.(); setTimeout(close, 900); }} /></div>`}
    <//>`}
  </div>`;
}
registerSW();
render(html`<${App} />`, document.getElementById("app"));
