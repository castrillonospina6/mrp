// Helpers de negocio compartidos por las acciones de escritura (submitRegistro,
// submitRegistroOperario, justificarPedido). Traducción fiel de code.js.

const TTL_EN_PROCESO_SEG = 120;
const TTL_IDEMPOTENCIA_SEG = 21600; // 6 horas — igual al tope que ya usaba CacheService

// Horas encadenadas del operario
export const HORA_LIMITE_ENVIO = "23:30";
export const TURNO_POR_DEFECTO = "06:00";

// Días hacia atrás que acepta submitRegistro. Debe coincidir con
// DIAS_MAX_ATRAS_REGISTRO en index.html (_resetFechaAdmin).
const DIAS_MAX_ATRAS_REGISTRO = 15;

// Sin uso desde 2026-09-28: se quitaron el piso del registro del operario y el
// tope del flujo administrativo. El umbral de "registro corto" NO vive aquí:
// es DURACION_CORTA_MIN en index.html, y solo decide si se muestra el modal de
// confirmación, no si el envío se rechaza. Estas dos quedan como registro de lo
// que hubo; si alguna vuelve a usarse, pasa por _calcularDuracionValidada
// (opciones.duracionMinimaMin / duracionMaximaMin), que sigue soportándolas.
export const DURACION_MINIMA_MIN = 5;
export const DURACION_MAXIMA_ADMIN_MIN = 720;

async function _sha256Hex(texto) {
  const buf = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(texto),
  );
  return [...new Uint8Array(buf)]
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

export async function _huellaPayload(accion, payload) {
  const datosOrdenados = {};
  Object.keys(payload)
    .sort()
    .forEach((key) => {
      let valor = payload[key];
      if (Array.isArray(valor)) valor = [...valor].sort();
      datosOrdenados[key] = valor;
    });
  return (
    "idem_" + (await _sha256Hex(accion + "|" + JSON.stringify(datosOrdenados)))
  );
}

// Reemplaza CacheService+LockService: el PRIMARY KEY de "idempotencia" hace de
// reserva atómica — si dos envíos idénticos llegan casi juntos, el segundo INSERT
// choca contra el primero sin necesidad de un lock explícito.
export async function _chequearIdempotencia(env, huella) {
  const ahora = Date.now();
  await env.DB.prepare(
    "DELETE FROM idempotencia WHERE huella = ? AND expira_en < ?",
  )
    .bind(huella, String(ahora))
    .run();

  try {
    await env.DB.prepare(
      "INSERT INTO idempotencia (huella, respuesta, expira_en) VALUES (?, '__EN_PROCESO__', ?)",
    )
      .bind(huella, String(ahora + TTL_EN_PROCESO_SEG * 1000))
      .run();
    return { duplicado: false, enProceso: false };
  } catch (err) {
    const existente = await env.DB.prepare(
      "SELECT respuesta FROM idempotencia WHERE huella = ?",
    )
      .bind(huella)
      .first();
    if (!existente) throw err;
    if (existente.respuesta === "__EN_PROCESO__")
      return { duplicado: false, enProceso: true };
    return { duplicado: true, respuesta: JSON.parse(existente.respuesta) };
  }
}

export async function _confirmarIdempotencia(env, huella, respuesta) {
  await env.DB.prepare(
    "UPDATE idempotencia SET respuesta = ?, expira_en = ? WHERE huella = ?",
  )
    .bind(
      JSON.stringify(respuesta),
      String(Date.now() + TTL_IDEMPOTENCIA_SEG * 1000),
      huella,
    )
    .run();
}

export async function _liberarIdempotencia(env, huella) {
  await env.DB.prepare("DELETE FROM idempotencia WHERE huella = ?")
    .bind(huella)
    .run();
}

export async function _validarSupervisorYSupernumerario(
  env,
  coordinadorEscrito,
  supernumerarioEscrito,
) {
  if (!coordinadorEscrito) throw new Error("Falta el nombre del coordinador.");
  if (!supernumerarioEscrito)
    throw new Error("Falta el nombre del supernumerario.");

  const coord = await env.DB.prepare(
    "SELECT nombre FROM supervisores WHERE tipo = 'coordinador' AND lower(nombre) = lower(?)",
  )
    .bind(coordinadorEscrito)
    .first();
  if (!coord)
    throw new Error(
      'El coordinador "' +
        coordinadorEscrito +
        '" no existe en el catálogo de supervisores.',
    );

  const supernum = await env.DB.prepare(
    "SELECT nombre FROM supervisores WHERE tipo = 'supernumerario' AND lower(nombre) = lower(?)",
  )
    .bind(supernumerarioEscrito)
    .first();
  if (!supernum)
    throw new Error(
      'El supernumerario "' +
        supernumerarioEscrito +
        '" no existe en el catálogo de supervisores.',
    );

  return { coordinador: coord.nombre, supernumerario: supernum.nombre };
}

// Devuelve las dos formas posibles de un EAN escaneado: la de 12 dígitos
// (real) y la de 13 con el "0" que antepone el preamble UPC-A por defecto
// del escáner Zebra (mismo problema que en code.js). Se usan ambas en el
// WHERE para que la fila coincida sin importar cuál quedó guardada en D1.
export function _formasEan(ean) {
  const valor = String(ean || "").trim();
  const sinPreambulo =
    valor.length === 13 && valor.charAt(0) === "0" ? valor.substring(1) : valor;
  const conPreambulo =
    sinPreambulo.length === 12 ? "0" + sinPreambulo : sinPreambulo;
  return [sinPreambulo, conPreambulo];
}

// Clave de comparación de pedidos: sin tildes, minúsculas y sin espacios.
export function _normalizarPedido(texto) {
  return String(texto || "")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/\s+/g, "");
}

// Resuelve lo escrito al texto exacto de numero_pedido en D1.
// Si no hay coincidencia única, devuelve el texto recortado sin cambios.
export async function _resolverPedido(env, texto) {
  const limpio = String(texto || "").trim();
  if (!limpio) return "";

  const exacto = await env.DB.prepare(
    "SELECT (EXISTS(SELECT 1 FROM validacion_eanes WHERE numero_pedido = ?) " +
      "OR EXISTS(SELECT 1 FROM registros WHERE numero_pedido = ?)) AS existe",
  )
    .bind(limpio, limpio)
    .first();
  if (exacto && exacto.existe) return limpio;

  const clave = _normalizarPedido(limpio);
  const { results } = await env.DB.prepare(
    "SELECT numero_pedido FROM validacion_eanes " +
      "UNION SELECT numero_pedido FROM registros",
  ).all();
  const coincidencias = results
    .map((r) => String(r.numero_pedido))
    .filter((p) => _normalizarPedido(p) === clave);

  return coincidencias.length === 1 ? coincidencias[0] : limpio;
}

export async function _localizarValidacionEan(env, pedido, ean) {
  const [eanA, eanB] = _formasEan(ean);
  const row = await env.DB.prepare(
    "SELECT sublinea, unidades_totales, sin_limite, unidades_procesadas, codigos_sobrantes, ean_producto, " +
      "(SELECT COALESCE(SUM(faltante),0) FROM justificaciones WHERE pedido = ? AND ean IN (?, ?)) AS justificado " +
      "FROM validacion_eanes WHERE numero_pedido = ? AND ean_producto IN (?, ?)",
  )
    .bind(pedido, eanA, eanB, pedido, eanA, eanB)
    .first();
  if (!row)
    throw new Error(
      "El pedido " +
        pedido +
        " con el EAN " +
        ean +
        " no existe en la hoja de Validación.",
    );
  return row;
}

export async function _obtenerDatosPedido(env, pedido, sublinea) {
  const row = await env.DB.prepare(
    "SELECT cliente, evento, clasificacion_proceso AS clasificacionProceso FROM pedidos " +
      "WHERE numero_pedido = ? AND sublinea = ?",
  )
    .bind(pedido, sublinea)
    .first();
  if (!row) {
    throw new Error(
      "El pedido " +
        pedido +
        ' con la sublínea "' +
        sublinea +
        '" no está registrado en Numero pedidos. Verifica que exista esa combinación exacta.',
    );
  }
  return row;
}

export async function _obtenerProcesoValidado(env, nombreProceso, pedido) {
  const nombre = String(nombreProceso || "").trim();
  if (!nombre) throw new Error("Falta seleccionar el proceso.");
  const row = await env.DB.prepare(
    "SELECT nombre_proceso AS nombre, unidades_hora_estandar AS unidadesHora FROM procesos " +
      "WHERE lower(nombre_proceso) = lower(?) AND numero_pedido = ?",
  )
    .bind(nombre, pedido)
    .first();
  if (!row)
    throw new Error(
      'El proceso "' +
        nombre +
        '" no existe o no está asignado al pedido ' +
        pedido +
        ".",
    );
  return row;
}

// Excluye las filas con cédula vacía (huérfanas que el pull no alcanza a borrar,
// porque el DELETE por 'cedula = ""' no matchea NULL en SQLite). Sin este filtro,
// un nombre presente en dos filas podía resolver contra la vacía y guardar el
// registro sin cédula: invisible para _inicioCadena y para Control tiempos.
// ORDER BY hace determinista cuál gana si el nombre está repetido.
export async function _buscarCedulaPorNombre(env, nombre) {
  const row = await env.DB.prepare(
    "SELECT cedula FROM operarios WHERE lower(nombre_operario) = lower(?) AND activo = 1 " +
      "AND cedula IS NOT NULL AND TRIM(cedula) <> '' ORDER BY cedula LIMIT 1",
  )
    .bind(nombre)
    .first();
  return row ? String(row.cedula).trim() : "";
}

// Equivalente a _marcarPedidoIniciado: la primera vez que un pedido recibe un
// registro, inicializa a 'Pendiente' los EANes que aún estén en blanco.
export async function _marcarPedidoIniciado(env, pedido, ahora) {
  const resultado = await env.DB.prepare(
    "UPDATE validacion_eanes SET estado = 'Pendiente', actualizado_en = ? " +
      "WHERE numero_pedido = ? AND (estado IS NULL OR estado = '')",
  )
    .bind(ahora, pedido)
    .run();

  if (resultado.meta.changes > 0) {
    await env.DB.prepare(
      "INSERT INTO control_produccion (numero_pedido, estado, actualizado_en) VALUES (?, 'En proceso', ?) " +
        "ON CONFLICT(numero_pedido) DO UPDATE SET estado = 'En proceso', actualizado_en = excluded.actualizado_en",
    )
      .bind(pedido, ahora)
      .run();
  }
}

export async function _pedidoEstaCompleto(env, pedido) {
  const row = await env.DB.prepare(
    // COALESCE: con estado NULL, \"NULL != 'Completado'\" da NULL y el EAN no se
    // contaba como pendiente. Los EANes nuevos entran desde el pull sin estado,
    // así que un pedido podía darse por completo con EANes sin tocar.
    "SELECT COUNT(*) AS total, SUM(CASE WHEN COALESCE(estado, '') != 'Completado' THEN 1 ELSE 0 END) AS pendientes " +
      "FROM validacion_eanes WHERE numero_pedido = ?",
  )
    .bind(pedido)
    .first();
  return row.total > 0 && row.pendientes === 0;
}

// Complementa _pedidoEstaCompleto: además de poder CERRAR un pedido (lo que ya
// hacen submitRegistro/submitRegistroOperario de forma inline), esta función
// también puede REABRIRLO si una edición o anulación hizo que ya no esté
// completo. La usan edición/anulación/reactivación de registros y
// justificarPedido; los dos flujos de envío siguen con su bloque inline.
export async function _sincronizarEstadoPedido(env, pedido) {
  const completo = await _pedidoEstaCompleto(env, pedido);
  const ahora = _ahoraISO();
  await env.DB.prepare(
    "INSERT INTO control_produccion (numero_pedido, estado, actualizado_en) VALUES (?, ?, ?) " +
      "ON CONFLICT(numero_pedido) DO UPDATE SET estado = excluded.estado, actualizado_en = excluded.actualizado_en",
  )
    .bind(pedido, completo ? "Completado" : "En proceso", ahora)
    .run();
  return completo;
}

// Fila de "Seguimiento paquetes" calculada desde la fuente en una sola
// sentencia (D1 ejecuta una por vez). ?1 se repite en todas las subconsultas.
// - Unds procesadas: sin códigos sobrantes, igual que Seguimiento de paquetes.
// - Unds pendientes: lo justificado; null si cerró sin faltante.
// - Eficiencia: misma fórmula que obtenerSeguimientoPaquetes.
// - Supernumerario: los de los registros activos.
// - Justificación: "quien justificó - motivo" (quien justificó se guarda en
//   justificaciones.supernumerario).
// GROUP_CONCAT(DISTINCT x, sep) no existe en SQLite: se deduplica en subconsulta.
export async function _datosSeguimientoPedido(env, pedido) {
  const r = await env.DB.prepare(
    "SELECT " +
      "(SELECT MIN(fecha) FROM pedidos WHERE numero_pedido = ?1 AND fecha <> '') AS fecha, " +
      "(SELECT GROUP_CONCAT(par, ' | ') FROM (SELECT DISTINCT p.sublinea || ' - ' || p.cliente AS par " +
      "FROM pedidos p WHERE p.numero_pedido = ?1 AND p.sublinea IN " +
      "(SELECT sublinea FROM validacion_eanes WHERE numero_pedido = ?1) ORDER BY par)) AS sublineaCliente, " +
      "(SELECT COALESCE(SUM(unidades_procesadas), 0) FROM validacion_eanes WHERE numero_pedido = ?1) AS procesadas, " +
      "(SELECT COALESCE(SUM(faltante), 0) FROM justificaciones WHERE pedido = ?1) AS justificado, " +
      "(SELECT COALESCE(SUM(tiempo_total * unidades_hora_estandar), 0) FROM registros " +
      "WHERE numero_pedido = ?1 AND anulado = 0) AS estandar, " +
      "(SELECT GROUP_CONCAT(nombre, ' - ') FROM (SELECT DISTINCT supernumerario AS nombre FROM registros " +
      "WHERE numero_pedido = ?1 AND anulado = 0 AND supernumerario <> '' ORDER BY nombre)) AS supernumerarios, " +
      "(SELECT GROUP_CONCAT(nombre, ' - ') FROM (SELECT DISTINCT coordinador AS nombre FROM registros " +
      "WHERE numero_pedido = ?1 AND anulado = 0 AND coordinador <> '' ORDER BY nombre)) AS supervisores, " +
      "(SELECT GROUP_CONCAT(texto, ' | ') FROM (SELECT DISTINCT CASE WHEN supernumerario <> '' " +
      "THEN supernumerario || ' - ' || motivo ELSE motivo END AS texto FROM justificaciones " +
      "WHERE pedido = ?1 AND motivo <> '')) AS justificacion",
  )
    .bind(pedido)
    .first();

  const procesadas = Number(r.procesadas) || 0;
  const justificado = Number(r.justificado) || 0;
  const estandar = Number(r.estandar) || 0;
  return {
    fecha: r.fecha || "",
    sublineaCliente: r.sublineaCliente || "",
    undsProcesadas: procesadas,
    undsPendientes: justificado > 0 ? justificado : null,
    eficiencia: estandar > 0 ? Math.round((procesadas / estandar) * 100) : null,
    supernumerarios: r.supernumerarios || "",
    supervisores: r.supervisores || "",
    estado: justificado > 0 ? "No cumple" : "Cumple",
    justificacion: r.justificacion || "",
  };
}

// Refleja en seguimiento_paquetes si el pedido está cerrado.
// - completo: escribe la fila fresca y la deja vigente. cerrado_en solo se fija
//   en la transición abierto → cerrado; una edición sobre un pedido que sigue
//   cerrado refresca los datos sin mover esa fecha.
// - no completo: si la fila estaba vigente, pasa a no vigente (F3 la borra de
//   la hoja hasta que el pedido vuelva a cerrar).
// Todo cambio deja sincronizado_sheets = 0 para que el push aplique el estado
// actual. Devuelve true si cambió algo.
export async function _actualizarSeguimientoPedido(env, pedido, completo) {
  const ahora = _ahoraISO();
  if (!completo) {
    const res = await env.DB.prepare(
      "UPDATE seguimiento_paquetes SET vigente = 0, sincronizado_sheets = 0, actualizado_en = ? " +
        "WHERE numero_pedido = ? AND vigente = 1",
    )
      .bind(ahora, pedido)
      .run();
    return res.meta.changes > 0;
  }

  const d = await _datosSeguimientoPedido(env, pedido);
  await env.DB.prepare(
    "INSERT INTO seguimiento_paquetes (numero_pedido, fecha, sublinea_cliente, unds_procesadas, " +
      "unds_pendientes, eficiencia, supernumerarios, supervisores, estado, justificacion, " +
      "vigente, sincronizado_sheets, cerrado_en, actualizado_en) " +
      "VALUES (?,?,?,?,?,?,?,?,?,?,1,0,?,?) " +
      "ON CONFLICT(numero_pedido) DO UPDATE SET fecha = excluded.fecha, " +
      "sublinea_cliente = excluded.sublinea_cliente, unds_procesadas = excluded.unds_procesadas, " +
      "unds_pendientes = excluded.unds_pendientes, eficiencia = excluded.eficiencia, " +
      "supernumerarios = excluded.supernumerarios, supervisores = excluded.supervisores, " +
      "estado = excluded.estado, justificacion = excluded.justificacion, " +
      "cerrado_en = CASE WHEN seguimiento_paquetes.vigente = 1 " +
      "THEN seguimiento_paquetes.cerrado_en ELSE excluded.cerrado_en END, " +
      "vigente = 1, sincronizado_sheets = 0, actualizado_en = excluded.actualizado_en",
  )
    .bind(
      pedido,
      d.fecha,
      d.sublineaCliente,
      d.undsProcesadas,
      d.undsPendientes,
      d.eficiencia,
      d.supernumerarios,
      d.supervisores,
      d.estado,
      d.justificacion,
      ahora,
      ahora,
    )
    .run();
  return true;
}

// Red de seguridad de seguimiento_paquetes, la llama el cron del Worker.
// 1) Pedidos cerrados (todos sus EANes Completado, ninguno sin límite) que no
//    tienen fila vigente → se les escribe. También hace el backfill.
// 2) Filas vigentes cuyo pedido sigue en validacion_eanes pero ya no está
//    cerrado → pasan a no vigentes. Si el pedido desapareció de
//    validacion_eanes (depuración), la fila NO se toca: la hoja es histórico.
// Topes por corrida: D1 limita las consultas por invocación (50 en el plan
// gratuito). 2 detecciones + 15 cierres × 2 + 10 reaperturas × 1 = 42. Lo que
// quede pendiente lo toma la siguiente corrida.
// El push a la hoja lo hace la reconciliación de Apps Script, porque todo lo
// que se toca aquí queda con sincronizado_sheets = 0.
export async function _reconciliarSeguimientoD1(env) {
  const { results: sinFila } = await env.DB.prepare(
    "SELECT v.numero_pedido FROM validacion_eanes v GROUP BY v.numero_pedido " +
      "HAVING SUM(CASE WHEN COALESCE(v.estado, '') <> 'Completado' THEN 1 ELSE 0 END) = 0 " +
      "AND MAX(v.sin_limite) = 0 " +
      "AND NOT EXISTS (SELECT 1 FROM seguimiento_paquetes s " +
      "WHERE s.numero_pedido = v.numero_pedido AND s.vigente = 1) " +
      "LIMIT 15",
  ).all();

  const { results: reabiertos } = await env.DB.prepare(
    "SELECT s.numero_pedido FROM seguimiento_paquetes s WHERE s.vigente = 1 " +
      "AND EXISTS (SELECT 1 FROM validacion_eanes v WHERE v.numero_pedido = s.numero_pedido " +
      "AND (COALESCE(v.estado, '') <> 'Completado' OR v.sin_limite = 1)) " +
      "LIMIT 10",
  ).all();

  for (const r of sinFila) {
    await _actualizarSeguimientoPedido(env, r.numero_pedido, true);
  }
  for (const r of reabiertos) {
    await _actualizarSeguimientoPedido(env, r.numero_pedido, false);
  }

  console.log(
    "Reconciliación seguimiento: " +
      sinFila.length +
      " cerrados sin fila" +
      (sinFila.length
        ? " (" + sinFila.map((r) => r.numero_pedido).join(", ") + ")"
        : "") +
      ", " +
      reabiertos.length +
      " reabiertos" +
      (reabiertos.length
        ? " (" + reabiertos.map((r) => r.numero_pedido).join(", ") + ")"
        : ""),
  );
}

// Revierte una reserva de unidades ya aplicada, para cuando el INSERT de
// registros falla DESPUÉS de que la reserva ya comprometió cambios en D1.
export async function _revertirReserva(
  env,
  { pedido, ean, total, codigosSobrantes },
) {
  await env.DB.prepare(
    "UPDATE validacion_eanes SET " +
      "unidades_procesadas = unidades_procesadas - ?, " +
      "codigos_sobrantes = codigos_sobrantes - ?, " +
      "estado = CASE " +
      "WHEN ((unidades_procesadas - ?) + (codigos_sobrantes - ?) + " +
      "(SELECT COALESCE(SUM(faltante),0) FROM justificaciones WHERE pedido = ? AND ean = ?)) <= 0 THEN 'Pendiente' " +
      "WHEN sin_limite = 0 AND ((unidades_procesadas - ?) + (codigos_sobrantes - ?) + " +
      "(SELECT COALESCE(SUM(faltante),0) FROM justificaciones WHERE pedido = ? AND ean = ?)) >= unidades_totales THEN 'Completado' " +
      "ELSE 'En proceso' END, " +
      "actualizado_en = ? " +
      "WHERE numero_pedido = ? AND ean_producto = ?",
  )
    .bind(
      total,
      codigosSobrantes,
      total,
      codigosSobrantes,
      pedido,
      ean,
      total,
      codigosSobrantes,
      pedido,
      ean,
      _ahoraISO(),
      pedido,
      ean,
    )
    .run();
}

// Reconstruye el contador de validacion_eanes desde la fuente real (registros +
// justificaciones) en una sola sentencia atómica, en vez de sumar/restar un
// delta a mano. Excluye registros anulados. Se usa SOLO desde edición/anulación
// de registros — submitRegistro y submitRegistroOperario NO usan esta función;
// su reserva atómica sigue exactamente igual.
export async function _recalcularContadorValidacion(env, pedido, ean) {
  const ahora = _ahoraISO();
  await env.DB.prepare(
    "WITH agregados AS (" +
      "SELECT COALESCE(SUM(unidades_procesadas_operario),0) AS procesado, " +
      "COALESCE(SUM(codigos_sobrantes),0) AS sobrantes " +
      "FROM registros WHERE numero_pedido = ? AND ean = ? AND anulado = 0" +
      "), justif AS (" +
      "SELECT COALESCE(SUM(faltante),0) AS justificado " +
      "FROM justificaciones WHERE pedido = ? AND ean = ?" +
      "), operarios_activos AS (" +
      "SELECT GROUP_CONCAT(nombre, ' - ') AS nombres FROM (" +
      "SELECT DISTINCT operario AS nombre FROM registros " +
      "WHERE numero_pedido = ? AND ean = ? AND anulado = 0)" +
      ") " +
      "UPDATE validacion_eanes SET " +
      "unidades_procesadas = (SELECT procesado FROM agregados), " +
      "codigos_sobrantes = (SELECT sobrantes FROM agregados), " +
      "operarios = COALESCE((SELECT nombres FROM operarios_activos), ''), " +
      "estado = CASE " +
      "WHEN sin_limite = 0 AND ((SELECT procesado FROM agregados) + (SELECT sobrantes FROM agregados) + (SELECT justificado FROM justif)) >= unidades_totales THEN 'Completado' " +
      "WHEN ((SELECT procesado FROM agregados) + (SELECT sobrantes FROM agregados) + (SELECT justificado FROM justif)) <= 0 THEN 'Pendiente' " +
      "ELSE 'En proceso' END, " +
      "actualizado_en = ? " +
      "WHERE numero_pedido = ? AND ean_producto = ?",
  )
    .bind(pedido, ean, pedido, ean, pedido, ean, ahora, pedido, ean)
    .run();
}

const PATRON_HORA = /^([01]\d|2[0-3]):([0-5]\d)$/;

// Antes, una hora vacía o mal formada producía NaN y ninguna guarda lo atrapaba
// (NaN <= 0, NaN > max y NaN < min son todas false): el registro se guardaba con
// duracion_bruta_min y tiempo_total en NaN, sin error.
function _minutosDeHora(valor, etiqueta) {
  const texto = String(valor == null ? "" : valor).trim();
  const m = PATRON_HORA.exec(texto);
  if (!m) {
    throw new Error(
      "La " +
        etiqueta +
        ' no es válida ("' +
        texto +
        '"). Usa el formato HH:MM de 24 horas.',
    );
  }
  return Number(m[1]) * 60 + Number(m[2]);
}

// Sin wrap de medianoche: el último envío es a las 23:30, así que una hora fin
// anterior a la de inicio es captura invertida, no un turno que cruza el día.
// El "+= 24 * 60" anterior convertía un bloque real de 15:00–18:00 mal digitado
// en un registro de 21 horas, silenciosamente.
export function _duracionMinutos(horaInicio, horaFin) {
  return (
    _minutosDeHora(horaFin, "hora de fin") -
    _minutosDeHora(horaInicio, "hora de inicio")
  );
}

export function _calcularDuracionValidada(
  horaInicio,
  horaFin,
  suplementoMin,
  opciones,
) {
  const minima = (opciones && opciones.duracionMinimaMin) || 0;
  const maxima = (opciones && opciones.duracionMaximaMin) || 0;

  const duracionBrutaMin = _duracionMinutos(horaInicio, horaFin);
  if (duracionBrutaMin < 0) {
    throw new Error(
      "La hora de fin (" +
        horaFin +
        ") es anterior a la de inicio (" +
        horaInicio +
        "). Revisa si los dos campos quedaron invertidos.",
    );
  }
  if (duracionBrutaMin === 0)
    throw new Error("La hora de fin debe ser posterior a la hora de inicio.");

  if (maxima && duracionBrutaMin > maxima) {
    throw new Error(
      "Este registro cubriría " +
        Math.round((duracionBrutaMin / 60) * 10) / 10 +
        " horas (" +
        horaInicio +
        " a " +
        horaFin +
        "), y el máximo permitido es " +
        maxima / 60 +
        " horas. Verifica que las horas no estén invertidas o mal digitadas.",
    );
  }

  // M6: el error de suplemento solo sale si es MAYOR al tiempo operado.
  if (suplementoMin > duracionBrutaMin) {
    throw new Error(
      "El suplemento (" +
        suplementoMin +
        " min) no puede ser mayor al tiempo trabajado (" +
        duracionBrutaMin +
        " min).",
    );
  }

  const duracionRealMin = duracionBrutaMin - suplementoMin;

  if (minima && duracionRealMin < minima) {
    throw new Error(
      "El registro es demasiado corto (" +
        duracionRealMin +
        " min de trabajo efectivo). El mínimo es " +
        minima +
        " minutos.",
    );
  }
  if (duracionRealMin <= 0)
    throw new Error("El suplemento no puede ser igual al tiempo trabajado.");

  return { duracionBrutaMin, duracionRealMin };
}

export function _calcularMetricasProduccion(
  unidadesOperario,
  tasaEstandarOperario,
  durHoras,
) {
  const esperado = tasaEstandarOperario * durHoras;
  const unidadesHoraReal =
    durHoras > 0 ? Math.round((unidadesOperario / durHoras) * 100) / 100 : 0;
  const eficiencia =
    esperado > 0 ? Math.round((unidadesOperario / esperado) * 100) : null;
  return { unidadesHoraReal, eficiencia };
}

export function _validarCodigosSobrantes(valor) {
  if (valor === undefined || valor === null || valor === "") return 0;
  const num = Number(valor);
  if (!Number.isInteger(num) || num < 0) {
    throw new Error(
      "Los códigos sobrantes deben ser un número entero mayor o igual a 0 (sin decimales).",
    );
  }
  return num;
}

// Fecha y hora de Bogotá de UN SOLO objeto Date: calcularlas por separado
// dejaría que un envío a las 23:59:59 quedara con la fecha de un día y la
// hora del otro. hourCycle "h23" evita el "24:xx" que algunos motores
// devuelven a medianoche con hour12: false.
export function _ahoraBogota() {
  const partes = new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/Bogota",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(new Date());

  const obtener = (tipo) => partes.find((p) => p.type === tipo).value;
  return {
    fecha: obtener("year") + "-" + obtener("month") + "-" + obtener("day"),
    hora: obtener("hour") + ":" + obtener("minute"),
  };
}

// Hora de inicio que le toca al próximo registro del operario: el fin de su
// último registro vigente del día, o su hora de turno si es el primero.
// Subconsultas escalares en una sola sentencia (D1 ejecuta una por vez).
export async function _inicioCadena(env, cedula, fecha) {
  const row = await env.DB.prepare(
    "SELECT (SELECT MAX(hora_final) FROM registros " +
      "WHERE cedula_operario = ? AND fecha = ? AND anulado = 0) AS ultimoFin, " +
      "(SELECT turno FROM operarios WHERE cedula = ?) AS turno",
  )
    .bind(cedula, fecha, cedula)
    .first();

  const ultimoFin = String((row && row.ultimoFin) || "").trim();
  if (ultimoFin) return { inicio: ultimoFin, turnoConfigurado: true };

  const turno = String((row && row.turno) || "").trim();
  if (turno) return { inicio: turno, turnoConfigurado: true };

  return { inicio: TURNO_POR_DEFECTO, turnoConfigurado: false };
}

export function _fechaHoyBogota() {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/Bogota",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
}

export function _validarFechaRegistro(fechaPayload) {
  const hoy = _fechaHoyBogota();
  const valor = String(fechaPayload || "").trim();
  if (!valor) return hoy;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(valor)) {
    throw new Error("La fecha del registro no tiene un formato válido.");
  }
  if (valor > hoy) {
    throw new Error("La fecha del registro no puede ser posterior a hoy.");
  }
  const limite = new Date(hoy + "T00:00:00Z");
  limite.setUTCDate(limite.getUTCDate() - DIAS_MAX_ATRAS_REGISTRO);
  const limiteStr = limite.toISOString().slice(0, 10);
  if (valor < limiteStr) {
    throw new Error(
      "La fecha del registro no puede ser anterior a " +
        limiteStr +
        " (máximo " +
        DIAS_MAX_ATRAS_REGISTRO +
        " días atrás).",
    );
  }
  return valor;
}

// formatToParts en vez de .format() para controlar el orden exacto del string
// y no depender de cómo el runtime arme el formato "en-CA" con hora incluida.
// Acepta una fecha/ISO opcional para reformatear un valor ya guardado en D1
// (anulado_en/editado_en); sin argumento, formatea "ahora" igual que antes.
export function _fechaHoraBogota(fecha) {
  const partes = new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/Bogota",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).formatToParts(fecha ? new Date(fecha) : new Date());

  const obtener = (tipo) => partes.find((p) => p.type === tipo).value;
  return (
    obtener("year") +
    "-" +
    obtener("month") +
    "-" +
    obtener("day") +
    " " +
    obtener("hour") +
    ":" +
    obtener("minute")
  );
}

export function _ahoraISO() {
  return new Date().toISOString();
}
