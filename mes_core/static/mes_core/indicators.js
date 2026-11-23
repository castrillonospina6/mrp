/* Indicadores en vivo: si el periodo incluye hoy, la pantalla se actualiza sola cada minuto (pausable). */
(function () {
  "use strict";
  const box = document.querySelector("[data-live]");
  if (!box) return;
  const every = (parseInt(box.dataset.every, 10) || 60) * 1000;
  const btn = box.querySelector("[data-live-toggle]");
  const label = box.querySelector("[data-live-label]");
  const next = box.querySelector("[data-live-next]");
  const KEY = "mes.indicators.paused";
  let paused = false;
  try { paused = localStorage.getItem(KEY) === "1"; } catch (e) { /* sin almacenamiento: no se recuerda */ }
  let due = Date.now() + every;

  function render() {
    box.classList.toggle("paused", paused);
    btn.textContent = paused ? "Reanudar" : "Pausar";
    btn.setAttribute("aria-pressed", paused ? "true" : "false");
    label.textContent = paused ? "En vivo (en pausa)" : "En vivo";
    if (paused) { next.textContent = "actualización automática en pausa"; return; }
    const s = Math.max(0, Math.round((due - Date.now()) / 1000));
    next.textContent = "se actualiza en " + s + " s";
  }

  btn.addEventListener("click", function () {
    paused = !paused;
    try { localStorage.setItem(KEY, paused ? "1" : "0"); } catch (e) { /* nada */ }
    due = Date.now() + every;
    render();
  });

  setInterval(function () {
    // con la pestaña oculta no se recarga; al volver, si ya tocaba, se recarga de una vez
    if (!paused && document.visibilityState === "visible" && Date.now() >= due) {
      const f = document.activeElement;
      if (f && f.matches("input, select, textarea")) { due = Date.now() + 15000; render(); return; } // escribiendo fechas
      location.reload();
      return;
    }
    render();
  }, 1000);
  document.addEventListener("visibilitychange", function () {
    if (!paused && document.visibilityState === "visible" && Date.now() >= due) location.reload();
  });
  render();
})();
