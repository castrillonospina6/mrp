// api.js — wrapper de comunicación con el backend (Registro de Producción)

// Único destino: el proxy del Worker de frontend (procesos-app), que intercepta
// POST /api, inyecta env.TOKEN_API y reenvía al backend por service binding.
// El token NO viaja desde aquí: el navegador nunca lo ve. El camino legado de
// Apps Script (/exec + token en texto plano) se eliminó el 09-10 — con él
// desaparece USAR_WORKER_NUEVO = false como vía de rollback, que de todos modos
// ya estaba roto (code.js submitRegistroOperario espera horas que el frontend
// nuevo no envía).
var API_URL = "/api";

function llamarAPI(action, datos, intento) {
  intento = intento || 1;
  var MAX_INTENTOS = 3;
  return fetch(API_URL + "?cachebust=" + Date.now(), {
    method: "POST",
    headers: { "Content-Type": "text/plain;charset=utf-8" },
    body: JSON.stringify({
      action: action,
      datos: datos || {},
    }),
    cache: "no-store",
  })
    .then(function (res) {
      return res.text();
    })
    .then(function (texto) {
      try {
        return JSON.parse(texto);
      } catch (e) {
        console.warn(
          "llamarAPI: respuesta no-JSON en intento " +
            intento +
            " (" +
            action +
            "). Primeros 200 caracteres:",
          texto.slice(0, 200),
        );
        if (intento < MAX_INTENTOS) {
          return new Promise(function (resolve) {
            setTimeout(function () {
              resolve(llamarAPI(action, datos, intento + 1));
            }, 500 * intento);
          });
        }
        throw new Error(
          "El servidor no respondió correctamente después de varios intentos. Verifica tu conexión e inténtalo de nuevo.",
        );
      }
    });
}
