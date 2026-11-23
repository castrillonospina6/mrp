/* Estructura de la aplicación: barra lateral (compacta / cajón móvil), tema y reloj de la planta. */
(() => {
  "use strict";
  const root = document.documentElement;
  const app = document.getElementById("app");
  if (!app) return;
  const store = (k, v) => {
    try {
      if (v === undefined) return localStorage.getItem(k);
      if (v === null) localStorage.removeItem(k); else localStorage.setItem(k, v);
    } catch { /* sin almacenamiento (modo privado) */ }
    return null;
  };

  // Barra lateral compacta (escritorio)
  document.getElementById("sb-collapse")?.addEventListener("click", () => {
    const mini = !root.classList.contains("sb-mini");
    root.classList.toggle("sb-mini", mini);
    store("mes.sidebar", mini ? "mini" : null);
    document.getElementById("sb-collapse").title = mini ? "Expandir menú" : "Contraer menú";
    window.dispatchEvent(new Event("resize"));
  });

  // Cajón (tableta / celular)
  const open = (yes) => {
    app.classList.toggle("open", yes);
    document.getElementById("sb-open")?.setAttribute("aria-expanded", String(yes));
  };
  document.getElementById("sb-open")?.addEventListener("click", () => open(true));
  document.getElementById("sb-overlay")?.addEventListener("click", () => open(false));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && app.classList.contains("open")) open(false); });
  document.querySelectorAll(".sb-nav a").forEach((a) => a.addEventListener("click", () => open(false)));

  // Tema: automático → claro → oscuro
  const THEMES = { auto: ["i-auto", "automático"], light: ["i-sun", "claro"], dark: ["i-moon", "oscuro"] };
  const themeBtn = document.getElementById("theme-btn");
  const paintTheme = (t) => {
    if (!themeBtn) return;
    const [icon, label] = THEMES[t];
    themeBtn.querySelector("use").setAttribute("href", `#${icon}`);
    themeBtn.querySelector("span").textContent = `Tema: ${label}`;
    themeBtn.title = `Tema: ${label} (clic para cambiar)`;
  };
  let theme = store("mes.theme") || store("theme") || "auto";
  if (!THEMES[theme]) theme = "auto";
  paintTheme(theme);
  themeBtn?.addEventListener("click", () => {
    theme = theme === "auto" ? "light" : theme === "light" ? "dark" : "auto";
    if (theme === "auto") root.removeAttribute("data-theme"); else root.setAttribute("data-theme", theme);
    store("mes.theme", theme === "auto" ? null : theme);
    store("theme", theme);  // mismo tema en el panel de administración
    paintTheme(theme);
  });

  // Reloj en la zona horaria de la planta
  const tz = window.MES_TZ || undefined;
  const dEl = document.getElementById("tb-date");
  const tEl = document.getElementById("tb-time");
  const fDate = new Intl.DateTimeFormat("es", { timeZone: tz, weekday: "short", day: "numeric", month: "short" });
  const fTime = new Intl.DateTimeFormat("es", { timeZone: tz, hour: "2-digit", minute: "2-digit", hour12: false });
  const tick = () => {
    const now = new Date();
    if (dEl) dEl.textContent = fDate.format(now);
    if (tEl) tEl.textContent = fTime.format(now);
  };
  tick();
  setInterval(tick, 15000);

  // ------------------------------------------------------------ sensación de inmediatez
  // 1) El menú se queda donde estaba al cambiar de página (no salta arriba).
  const nav = document.querySelector(".sb-nav");
  try { const y = +sessionStorage.getItem("mes.sbScroll"); if (nav && y) nav.scrollTop = y; } catch { /* sin almacenamiento */ }
  addEventListener("pagehide", () => { try { if (nav) sessionStorage.setItem("mes.sbScroll", String(nav.scrollTop)); } catch { /* */ } });

  // 2) Barra de progreso arriba: solo aparece si la página tarda (la mayoría abre antes y no se ve).
  const bar = document.getElementById("nprog");
  let barT = 0;
  const startBar = () => { clearTimeout(barT); barT = setTimeout(() => bar && bar.classList.add("on"), 140); };
  const stopBar = () => { clearTimeout(barT); bar && bar.classList.remove("on"); };
  addEventListener("pageshow", () => { stopBar(); document.querySelectorAll(".is-busy").forEach(unbusy); document.querySelectorAll("form[data-busy]").forEach((f) => delete f.dataset.busy); });

  // 3) Clic en un enlace: el menú marca la opción al instante y arranca la barra.
  document.addEventListener("click", (e) => {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const a = e.target.closest("a[href]");
    if (!a || a.target === "_blank" || a.hasAttribute("download")) return;
    const u = new URL(a.href, location.href);
    if (u.origin !== location.origin || (u.pathname === location.pathname && u.search === location.search && u.hash)) return;
    if (a.classList.contains("sb-link")) {
      document.querySelectorAll(".sb-link.on").forEach((x) => x.classList.remove("on"));
      a.classList.add("on");
    }
    startBar();
  });

  // 4) Botones de formularios: reaccionan en el acto (girito) y no se envía dos veces por doble clic.
  function unbusy(b) { b.classList.remove("is-busy"); b.removeAttribute("aria-busy"); }
  document.addEventListener("submit", (e) => {
    const f = e.target;
    if (f.dataset.busy) { e.preventDefault(); return; }   // ya se está enviando
    const btn = e.submitter || f.querySelector("button[type=submit], button:not([type])");
    // se espera a que terminen los demás manejadores: si alguno lo canceló (confirmación, envío por JS) no se toca nada
    setTimeout(() => {
      if (e.defaultPrevented || f.target === "_blank" || (btn && btn.formTarget === "_blank")) return;
      f.dataset.busy = "1";
      if (btn) { btn.classList.add("is-busy"); btn.setAttribute("aria-busy", "true"); }
      startBar();
      // descargas (Excel, CSV): la página no cambia, el botón vuelve solo
      const dl = f.hasAttribute("data-download") || /descargar|exportar|\.csv|\.xlsx/.test((btn && btn.getAttribute("formaction")) || f.action || "")
        || (btn && btn.name === "descargar");
      setTimeout(() => { delete f.dataset.busy; if (btn) unbusy(btn); stopBar(); }, dl ? 2500 : 12000);
    }, 0);
  });
})();
