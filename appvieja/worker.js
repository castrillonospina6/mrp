// worker.js — proxy que agrega el token real antes de reenviar al backend.
// El navegador nunca ve TOKEN_API: vive solo como secret de este Worker.

const BACKEND_URL = "https://registro-produccion-api.glovanze.workers.dev/";

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/api" && request.method === "POST") {
      let payload;
      try {
        payload = JSON.parse(await request.text());
      } catch (e) {
        return new Response(
          JSON.stringify({
            success: false,
            message: "Cuerpo de solicitud inválido",
          }),
          { status: 400, headers: { "Content-Type": "application/json" } },
        );
      }

      payload.token = env.TOKEN_API;

      try {
        const respuesta = await env.BACKEND.fetch(
          BACKEND_URL + "?cachebust=" + Date.now(),
          {
            method: "POST",
            headers: { "Content-Type": "text/plain;charset=utf-8" },
            body: JSON.stringify(payload),
          },
        );
        const texto = await respuesta.text();
        return new Response(texto, {
          status: respuesta.status,
          headers: { "Content-Type": "application/json" },
        });
      } catch (e) {
        return new Response(
          JSON.stringify({
            success: false,
            message: "No se pudo contactar el backend",
          }),
          { status: 502, headers: { "Content-Type": "application/json" } },
        );
      }
    }

    return env.ASSETS.fetch(request);
  },
};
