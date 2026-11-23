// Push best-effort D1 → Sheets. Si falla (red, Apps Script caído, etc.), no
// interrumpe la respuesta al usuario — la reconciliación periódica (paso 7b)
// cubre cualquier fila que quede con sincronizado_sheets = 0.

import { _fechaHoraBogota } from "./d1-negocio.js";

export async function _pushRegistrosYMarcar(env, ids, filas) {
  if (!ids || ids.length === 0) return;
  try {
    const res = await fetch(env.APPS_SCRIPT_URL, {
      method: "POST",
      headers: { "Content-Type": "text/plain;charset=utf-8" },
      body: JSON.stringify({
        action: "_pushRegistros",
        token: env.APPS_SCRIPT_TOKEN,
        datos: { filas },
      }),
    });
    const body = await res.json();
    if (!body.success) return;

    const placeholders = ids.map(() => "?").join(",");
    await env.DB.prepare(
      "UPDATE registros SET sincronizado_sheets = 1, sincronizado_en = ? WHERE id IN (" +
        placeholders +
        ")",
    )
      .bind(new Date().toISOString(), ...ids)
      .run();
  } catch (err) {
    // best-effort: se queda con sincronizado_sheets = 0, la reconciliación lo recoge después.
  }
}

// Relee la fila completa y fresca de D1 (ya con los cambios de editar/anular/
// reactivar aplicados), calcula "Última acción" según el estado actual, y
// sobreescribe la fila entera en Sheets.
export async function _pushFilaRegistroYMarcar(env, idRegistro) {
  try {
    const fila = await env.DB.prepare(
      "SELECT id, lote_id, fecha, coordinador, cedula_operario, operario, supernumerario, " +
        "cliente, proceso, unidades_hora_estandar, sublinea, evento, numero_pedido, ean, " +
        "unidades_totales_procesadas, hora_inicio, hora_final, suplemento, observacion, " +
        "duracion_bruta_min, clasificacion_proceso, tiempo_total, unidades_procesadas_operario, " +
        "unidades_hora_real, eficiencia, anulado, anulado_por, anulado_en, motivo_anulacion, " +
        "editado_por, editado_en, motivo_edicion FROM registros WHERE id = ?",
    )
      .bind(idRegistro)
      .first();
    if (!fila) return; // se borró justo antes de que este push corriera

    const ultimaAccion =
      fila.anulado === 1
        ? "ANULADO — " +
          fila.anulado_por +
          " — " +
          fila.motivo_anulacion +
          " — " +
          _fechaHoraBogota(fila.anulado_en)
        : fila.editado_por
          ? "EDITADO — " +
            fila.editado_por +
            " — " +
            fila.motivo_edicion +
            " — " +
            _fechaHoraBogota(fila.editado_en)
          : "";

    const valores = [
      fila.lote_id,
      fila.fecha,
      fila.coordinador,
      fila.cedula_operario,
      fila.operario,
      fila.supernumerario,
      fila.cliente,
      fila.proceso,
      fila.unidades_hora_estandar,
      fila.sublinea,
      fila.evento,
      fila.numero_pedido,
      fila.ean,
      fila.unidades_totales_procesadas,
      fila.hora_inicio,
      fila.hora_final,
      fila.suplemento,
      fila.observacion,
      fila.duracion_bruta_min,
      fila.clasificacion_proceso,
      fila.tiempo_total,
      fila.unidades_procesadas_operario,
      fila.unidades_hora_real,
      fila.eficiencia,
      fila.id,
      ultimaAccion,
    ];

    const res = await fetch(env.APPS_SCRIPT_URL, {
      method: "POST",
      headers: { "Content-Type": "text/plain;charset=utf-8" },
      body: JSON.stringify({
        action: "_actualizarFilaRegistro",
        token: env.APPS_SCRIPT_TOKEN,
        datos: { idRegistro, valores },
      }),
    });
    const body = await res.json();
    if (!body.success) return;

    await env.DB.prepare(
      "UPDATE registros SET anulado_sincronizado_sheets = 1 WHERE id = ?",
    )
      .bind(idRegistro)
      .run();
  } catch (err) {
    // best-effort: se queda con anulado_sincronizado_sheets = 0, la reconciliación lo recoge después.
  }
}

// Ya no es best-effort ciego: eliminarRegistro deja la fila en
// eliminaciones_pendientes antes de llamar aquí. Esta función la saca de la
// cola solo si la hoja confirmó el borrado; si no, deja el motivo escrito
// para que la reconciliación reintente y el fallo sea visible en D1.
export async function _eliminarFilaRegistroSheets(env, idRegistro, loteId) {
  let motivo = "";
  try {
    const res = await fetch(env.APPS_SCRIPT_URL, {
      method: "POST",
      headers: { "Content-Type": "text/plain;charset=utf-8" },
      body: JSON.stringify({
        action: "_eliminarFilaRegistro",
        token: env.APPS_SCRIPT_TOKEN,
        datos: { idRegistro, loteId },
      }),
    });
    const body = await res.json();

    // "ID D1 no encontrado" = la fila ya no está en la hoja (purga previa, o
    // un borrado anterior que sí funcionó): el objetivo ya se cumplió.
    if (body.success || body.message === "ID D1 no encontrado en la hoja.") {
      await env.DB.prepare(
        "DELETE FROM eliminaciones_pendientes WHERE id_registro = ?",
      )
        .bind(idRegistro)
        .run();
      return;
    }
    motivo = String(body.message || "respuesta sin success");
  } catch (err) {
    motivo = "push: " + (err && err.message ? err.message : String(err));
  }

  try {
    await env.DB.prepare(
      "UPDATE eliminaciones_pendientes SET intentos = intentos + 1, " +
        "ultimo_error = ? WHERE id_registro = ?",
    )
      .bind(motivo.slice(0, 300), idRegistro)
      .run();
  } catch (err) {
    // La fila ya quedó encolada por eliminarRegistro; guardar el motivo es
    // deseable pero no crítico — la reconciliación la recoge igual.
  }
}

// Relee la fila fresca de D1 y la manda a la hoja. El estado manda: vigente = 1
// escribe, vigente = 0 borra. La marca de sincronizado va condicionada a
// actualizado_en, así que si el pedido cambió entre el SELECT y el UPDATE la
// bandera se queda en 0 y la reconciliación de 10 min lo recoge.
export async function _pushSeguimientoYMarcar(env, pedido) {
  try {
    const f = await env.DB.prepare(
      "SELECT fecha, sublinea_cliente, unds_procesadas, unds_pendientes, eficiencia, " +
        "supernumerarios, supervisores, estado, justificacion, vigente, actualizado_en " +
        "FROM seguimiento_paquetes WHERE numero_pedido = ?",
    )
      .bind(pedido)
      .first();
    if (!f) return;

    // El orden debe coincidir con HEADERS_SEGUIMIENTO_PAQUETES de code.js.
    const fila = [
      f.fecha || "", // ISO; code.js lo pasa a dd/mm/aaaa
      f.sublinea_cliente || "",
      pedido,
      f.unds_procesadas,
      f.unds_pendientes === null ? "" : f.unds_pendientes,
      f.eficiencia === null ? "" : f.eficiencia,
      f.supernumerarios || "",
      f.supervisores || "",
      f.estado,
      f.justificacion || "",
    ];

    const res = await fetch(env.APPS_SCRIPT_URL, {
      method: "POST",
      headers: { "Content-Type": "text/plain;charset=utf-8" },
      body: JSON.stringify({
        action: "_pushSeguimientoPaquete",
        token: env.APPS_SCRIPT_TOKEN,
        datos: { fila, vigente: f.vigente === 1 },
      }),
    });
    const body = await res.json();
    if (!body.success) return;

    await env.DB.prepare(
      "UPDATE seguimiento_paquetes SET sincronizado_sheets = 1 " +
        "WHERE numero_pedido = ? AND actualizado_en = ?",
    )
      .bind(pedido, f.actualizado_en)
      .run();
  } catch (err) {
    // best-effort: se queda con sincronizado_sheets = 0.
  }
}
