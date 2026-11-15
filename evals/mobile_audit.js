// Mobile audit: run in the page (DevTools console, Playwright `page.evaluate`, or the browser pane)
// at each phone width. Visits each screen and reports what would feel broken on a phone.
//
//   - horizontal overflow (sideways scrolling)
//   - tap targets under 44x44 px (Apple HIG / Material minimum)
//   - text under 11 px
//   - inputs under 16 px (iOS Safari zooms the page when they get focus)
//   - PWA basics: viewport meta, manifest, service worker
async function finnMobileAudit(routes = ["/", "/c/DEEPAKNTR", "/c/DIXON", "/research", "/people", "/chat"]) {
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const out = { width: innerWidth, screens: {} };
  for (const route of routes) {
    location.hash = route;
    await wait(route.startsWith("/c/") ? 2500 : 1200);
    const vis = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el); return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none"; };
    // SVG text is drawn in viewBox units; its on-screen size is declared size x the SVG's scale.
    const rendered = (el) => {
      const fs = parseFloat(getComputedStyle(el).fontSize);
      const svg = el.ownerSVGElement;
      if (!svg || !svg.viewBox.baseVal.width) return fs;
      return fs * svg.getBoundingClientRect().width / svg.viewBox.baseVal.width;
    };
    const inline = (el) => el.matches(".cite, .linkish, .tick, a") && getComputedStyle(el).display.startsWith("inline");
    const small = [...document.querySelectorAll("button, a[href], input, [role=switch], [role=tab]")]
      .filter((el) => vis(el) && !inline(el))
      .map((el) => ({ el, r: el.getBoundingClientRect() }))
      .filter(({ r }) => Math.round(r.width) < 44 || Math.round(r.height) < 44)
      .map(({ el, r }) => `${el.tagName.toLowerCase()}.${(el.className || "").toString().split(" ")[0]} "${(el.innerText || el.getAttribute("aria-label") || "").trim().slice(0, 18)}" ${Math.round(r.width)}x${Math.round(r.height)}`);
    const tinyText = [...document.querySelectorAll("body *")].filter((el) => vis(el) && el.childNodes.length && [...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim())
      && rendered(el) < 11).map((el) => `${el.tagName.toLowerCase()}.${(el.className?.baseVal ?? el.className ?? "").toString().split(" ")[0]} ${rendered(el).toFixed(1)}px`);
    const zoomInputs = [...document.querySelectorAll("input, textarea, select")].filter((el) => vis(el) && parseFloat(getComputedStyle(el).fontSize) < 16).length;
    out.screens[route] = {
      overflow_px: Math.max(0, document.documentElement.scrollWidth - document.documentElement.clientWidth),
      small_targets: small.length, small_examples: [...new Set(small)].slice(0, 4),
      tiny_text: tinyText.length, tiny_examples: [...new Set(tinyText)].slice(0, 3),
      zoom_inputs: zoomInputs,
    };
  }
  out.pwa = {
    viewport: !!document.querySelector('meta[name=viewport][content*="width=device-width"]'),
    manifest: !!document.querySelector('link[rel=manifest]'),
    service_worker: !!(await navigator.serviceWorker?.getRegistration()),
    apple_touch_icon: !!document.querySelector('link[rel=apple-touch-icon]'),
  };
  location.hash = "/";
  return out;
}
