const DEMORA_MAX_SEG = 10; // techo de demora tras intentos fallidos repetidos
const DECAY_MS = 60 * 60 * 1000; // 1 hora sin fallos nuevos = contador se considera reiniciado

export const accionesAuth = {
  async validarLogin(datos, env, ctx, ip) {
    const rol = String(datos.rol || "")
      .trim()
      .toLowerCase();
    const clave = String(datos.password || "");

    if (rol === "operario") {
      // El perfil operario no requiere contraseña.
      return { success: true, rol };
    }

    if (rol !== "supervisor") {
      return { success: false, message: "Rol inválido." };
    }

    const claveGuardada = env.PASS_SUPERVISOR || "";
    if (!claveGuardada) {
      return {
        success: false,
        message:
          "No hay contraseña configurada para este rol. Contacta al administrador.",
      };
    }

    const identificador = ip || "desconocida";
    const fila = await env.DB.prepare(
      "SELECT intentos, actualizado_en FROM intentos_login WHERE identificador = ?",
    )
      .bind(identificador)
      .first();
    const yaExpiro =
      fila && Date.now() - new Date(fila.actualizado_en).getTime() > DECAY_MS;
    const intentosPrevios = fila && !yaExpiro ? fila.intentos : 0;

    if (clave === claveGuardada) {
      if (fila) {
        await env.DB.prepare(
          "DELETE FROM intentos_login WHERE identificador = ?",
        )
          .bind(identificador)
          .run();
      }
      return { success: true, rol };
    }

    // Contraseña incorrecta: registrar el intento y aplicar demora creciente
    // (no bloqueo duro, para no afectar a otros dispositivos que compartan IP).
    const intentosNuevos = intentosPrevios + 1;
    await env.DB.prepare(
      "INSERT INTO intentos_login (identificador, intentos, actualizado_en) VALUES (?, ?, ?) " +
        "ON CONFLICT(identificador) DO UPDATE SET intentos = excluded.intentos, actualizado_en = excluded.actualizado_en",
    )
      .bind(identificador, intentosNuevos, new Date().toISOString())
      .run();

    const demoraSeg = Math.min(intentosNuevos, DEMORA_MAX_SEG);
    await new Promise((resolve) => setTimeout(resolve, demoraSeg * 1000));

    return { success: false, message: "Contraseña incorrecta." };
  },
};
