// Mobile-first plumbing: gestures, install, push, offline, haptics.
import { useEffect, useRef, useState } from "/static/vendor/preact-htm.module.js";

export const haptic = (ms = 12) => { try { navigator.vibrate?.(ms); } catch {} };
export const isStandalone = () => matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
export const isIOS = () => /iphone|ipad|ipod/i.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);

// ---------------------------------------------------------------------------
// Service worker + install
// ---------------------------------------------------------------------------
let deferredPrompt = null;
const installListeners = new Set();
window.addEventListener("beforeinstallprompt", (e) => { e.preventDefault(); deferredPrompt = e; installListeners.forEach((f) => f(true)); });
window.addEventListener("appinstalled", () => { deferredPrompt = null; installListeners.forEach((f) => f(false)); });

export function registerSW() {
  if (!("serviceWorker" in navigator)) return;
  const hadController = !!navigator.serviceWorker.controller;
  navigator.serviceWorker.register("/sw.js").then((reg) => reg.update()).catch(() => {});
  // A new release took over: reload once so this screen runs the new code, not the old cache.
  let reloaded = false;
  navigator.serviceWorker.addEventListener("controllerchange", () => {
    if (hadController && !reloaded) { reloaded = true; location.reload(); }
  });
}

/** {canPrompt, iosHint, install()}: Android/desktop get the native prompt; iOS gets instructions. */
export function useInstall() {
  const [canPrompt, setCan] = useState(!!deferredPrompt);
  useEffect(() => { installListeners.add(setCan); return () => installListeners.delete(setCan); }, []);
  const dismissed = (() => { try { return localStorage.getItem("finn.installHint") === "no"; } catch { return false; } })();
  return {
    canPrompt: canPrompt && !isStandalone(),
    iosHint: isIOS() && !isStandalone() && !dismissed,
    install: async () => { if (!deferredPrompt) return; deferredPrompt.prompt(); await deferredPrompt.userChoice; deferredPrompt = null; setCan(false); },
    hideHint: () => { try { localStorage.setItem("finn.installHint", "no"); } catch {} },
  };
}

// ---------------------------------------------------------------------------
// Push notifications
// ---------------------------------------------------------------------------
const b64ToBytes = (b64) => {
  const pad = "=".repeat((4 - (b64.length % 4)) % 4);
  const raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
};

export async function pushState() {
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) return { supported: false, reason: isIOS() && !isStandalone() ? "ios-install" : "unsupported" };
  const reg = await navigator.serviceWorker.ready;
  const sub = await reg.pushManager.getSubscription();
  return { supported: true, on: !!sub, permission: Notification.permission };
}

export async function enablePush() {
  const perm = await Notification.requestPermission();
  if (perm !== "granted") throw new Error("Notifications are blocked for FINN in this browser's settings.");
  const reg = await navigator.serviceWorker.ready;
  const { publicKey } = await (await fetch("/api/push/key")).json();
  const sub = (await reg.pushManager.getSubscription()) || await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(publicKey) });
  const r = await fetch("/api/push/subscribe", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(sub.toJSON()) });
  if (!r.ok) throw new Error("Couldn't register this device.");
  return r.json();
}

export async function disablePush() {
  const reg = await navigator.serviceWorker.ready;
  const sub = await reg.pushManager.getSubscription();
  if (!sub) return;
  await fetch("/api/push/subscribe", { method: "DELETE", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ endpoint: sub.endpoint }) });
  await sub.unsubscribe();
}

// ---------------------------------------------------------------------------
// Online / offline
// ---------------------------------------------------------------------------
export function useOnline() {
  const [on, setOn] = useState(navigator.onLine);
  useEffect(() => {
    const up = () => setOn(true), down = () => setOn(false);
    addEventListener("online", up); addEventListener("offline", down);
    return () => { removeEventListener("online", up); removeEventListener("offline", down); };
  }, []);
  return on;
}

// ---------------------------------------------------------------------------
// Gestures
// ---------------------------------------------------------------------------
/** Horizontal swipe on an element; calls onSwipe(direction) past the threshold. Vertical scroll stays native. */
export function useSwipe(onSwipe, { threshold = 0.32 } = {}) {
  const ref = useRef(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    let x0 = 0, y0 = 0, dx = 0, dragging = false, decided = false;
    const down = (e) => {
      if (e.pointerType === "mouse" || e.target.closest("a,button,input,.cite,svg")) return;
      x0 = e.clientX; y0 = e.clientY; dx = 0; dragging = true; decided = false;
    };
    const move = (e) => {
      if (!dragging) return;
      const mx = e.clientX - x0, my = e.clientY - y0;
      if (!decided) {
        if (Math.abs(mx) < 8 && Math.abs(my) < 8) return;
        decided = true;
        if (Math.abs(my) > Math.abs(mx)) { dragging = false; return; }  // a scroll, not a swipe
        try { el.setPointerCapture?.(e.pointerId); } catch {}  // not all pointers are capturable
      }
      dx = mx;
      el.style.transition = "none";
      el.style.transform = `translateX(${dx}px)`;
      el.style.opacity = String(1 - Math.min(0.6, Math.abs(dx) / el.offsetWidth));
      el.dataset.swipe = dx > 0 ? "right" : "left";
    };
    const up = () => {
      if (!dragging) return;
      dragging = false;
      const done = Math.abs(dx) > el.offsetWidth * threshold;
      el.style.transition = "transform .22s ease, opacity .22s ease";
      if (done) {
        el.style.transform = `translateX(${dx > 0 ? "110%" : "-110%"})`;
        el.style.opacity = "0";
        haptic();
        setTimeout(() => onSwipe(dx > 0 ? "right" : "left"), 200);
      } else {
        el.style.transform = ""; el.style.opacity = ""; delete el.dataset.swipe;
      }
    };
    el.addEventListener("pointerdown", down);
    el.addEventListener("pointermove", move);
    el.addEventListener("pointerup", up);
    el.addEventListener("pointercancel", up);
    return () => { el.removeEventListener("pointerdown", down); el.removeEventListener("pointermove", move); el.removeEventListener("pointerup", up); el.removeEventListener("pointercancel", up); };
  }, [onSwipe]);
  return ref;
}

/** Pull down at the top of the page to refresh. Returns pull distance for the indicator. */
export function usePullToRefresh(onRefresh) {
  const [pull, setPull] = useState(0);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let y0 = null, d = 0;
    const start = (e) => { if (window.scrollY <= 0 && !busy) { y0 = e.touches[0].clientY; d = 0; } };
    const move = (e) => {
      if (y0 == null) return;
      d = Math.max(0, e.touches[0].clientY - y0);
      if (d > 0 && window.scrollY <= 0) setPull(Math.min(110, d * 0.5));
    };
    const end = async () => {
      if (y0 == null) return;
      y0 = null;
      if (d * 0.5 > 64) { haptic(18); setBusy(true); setPull(56); try { await onRefresh(); } finally { setBusy(false); setPull(0); } }
      else setPull(0);
    };
    addEventListener("touchstart", start, { passive: true });
    addEventListener("touchmove", move, { passive: true });
    addEventListener("touchend", end);
    return () => { removeEventListener("touchstart", start); removeEventListener("touchmove", move); removeEventListener("touchend", end); };
  }, [onRefresh, busy]);
  return { pull, busy };
}

/** Drag a bottom sheet down to close it. */
export function useSheetDrag(onClose) {
  const ref = useRef(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    let y0 = null, d = 0;
    const start = (e) => { if (el.scrollTop <= 0) { y0 = e.touches[0].clientY; d = 0; } };
    const move = (e) => {
      if (y0 == null) return;
      d = e.touches[0].clientY - y0;
      if (d > 0) { el.style.transition = "none"; el.style.transform = `translateY(${d}px)`; }
    };
    const end = () => {
      if (y0 == null) return;
      y0 = null;
      el.style.transition = "transform .2s ease";
      if (d > 110) { el.style.transform = "translateY(100%)"; haptic(); setTimeout(onClose, 180); }
      else el.style.transform = "";
    };
    el.addEventListener("touchstart", start, { passive: true });
    el.addEventListener("touchmove", move, { passive: true });
    el.addEventListener("touchend", end);
    return () => { el.removeEventListener("touchstart", start); el.removeEventListener("touchmove", move); el.removeEventListener("touchend", end); };
  }, [onClose]);
  return ref;
}
