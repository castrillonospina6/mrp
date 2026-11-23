import {
  _huellaPayload,
  _chequearIdempotencia,
  _confirmarIdempotencia,
  _liberarIdempotencia,
  _validarSupervisorYSupernumerario,
  _localizarValidacionEan,
  _obtenerDatosPedido,
  _obtenerProcesoValidado,
  _buscarCedulaPorNombre,
  _calcularDuracionValidada,
  _ahoraBogota,
  _inicioCadena,
  HORA_LIMITE_ENVIO,
  _calcularMetricasProduccion,
  _validarCodigosSobrantes,
  _marcarPedidoIniciado,
  _pedidoEstaCompleto,
  _fechaHoyBogota,
  _validarFechaRegistro,
  _ahoraISO,
  _revertirReserva,
  _recalcularContadorValidacion,
  _sincronizarEstadoPedido,
  _actualizarSeguimientoPedido,
  _resolverPedido,
} from "./d1-negocio.js";

import {
  _pushRegistrosYMarcar,
  _pushFilaRegistroYMarcar,
  _eliminarFilaRegistroSheets,
  _pushSeguimientoYMarcar,
} from "./push-sheets.js";

export const accionesEscritura = {
  async submitRegistro(payload, env, ctx) {
    const huella = await _huellaPayload("submitRegistro", payload);
    const chequeo = await _chequearIdempotencia(env, huella);
    if (chequeo.duplicado) return chequeo.respuesta;
    if (chequeo.enProceso) {
      return {
        success: false,
        message:
          "Este registro ya se está procesando (posible doble envío). Espera unos segundos y verifica en Registros antes de reintentar.",
      };
    }

    let reservaAplicada = null;

    try {
      const pedido = await _resolverPedido(env, payload.pedido);
      if (!pedido) throw new Error("Falta el número de pedido.");

      let codigoEan = String(payload.codigoEan || "").trim();
      if (!codigoEan)
        throw new Error("Falta escanear el código EAN del producto.");

      const { coordinador, supernumerario } =
        await _validarSupervisorYSupernumerario(
          env,
          String(payload.coordinador || "").trim(),
          String(payload.supernumerario || "").trim(),
        );

      const observaciones = String(payload.observaciones || "").trim();
      const operarios = (payload.operarios || [])
        .map((n) => String(n).trim())
        .filter(Boolean);
      const n = operarios.length;
      if (n === 0) throw new Error("Debes agregar al menos un operario.");

      const total = Number(payload.totalUnidades);
      if (!Number.isFinite(total) || total < 0)
        throw new Error("Total de unidades inválido.");

      const validacion = await _localizarValidacionEan(env, pedido, codigoEan);
      // De aquí en adelante se usa el EAN tal como quedó en D1 (ean_producto),
      // no el crudo del escáner — evita que el "0" del preamble UPC-A rompa
      // la reserva/INSERT si el catálogo tiene la otra forma.
      codigoEan = validacion.ean_producto;
      const unidadesEsperadas = validacion.unidades_totales;
      const sublinea = validacion.sublinea;
      const datosPedido = await _obtenerDatosPedido(env, pedido, sublinea);

      const codigosSobrantes = _validarCodigosSobrantes(
        payload.codigosSobrantes,
      );
      const restantes =
        unidadesEsperadas -
        validacion.unidades_procesadas -
        validacion.justificado -
        validacion.codigos_sobrantes;

      if (!validacion.sin_limite && total + codigosSobrantes > restantes) {
        throw new Error(
          "La cantidad ingresada (" +
            total +
            (codigosSobrantes > 0
              ? " unidades + " + codigosSobrantes + " códigos sobrantes"
              : " unidades") +
            ") supera lo que falta del pedido (" +
            restantes +
            " de " +
            unidadesEsperadas +
            " totales). Verifica antes de continuar.",
        );
      }

      const suplementoMin = Number(payload.suplementoMin) || 0;

      // Sin tope de duración: coherente con la edición y con el flujo operario.
      const { duracionBrutaMin, duracionRealMin } = _calcularDuracionValidada(
        payload.horaInicio,
        payload.horaFin,
        suplementoMin,
      );
      const procesoValidado = await _obtenerProcesoValidado(
        env,
        payload.proceso,
        pedido,
      );
      const tasaEstandar = procesoValidado.unidadesHora;
      const loteId = crypto.randomUUID();
      const fecha = _validarFechaRegistro(payload.fecha);
      const ahora = _ahoraISO();

      const unidadesPorOperario = Math.round((total / n) * 100) / 100;
      const codigosSobrantesPorOperario =
        Math.round((codigosSobrantes / n) * 100) / 100;
      const durHoras = Math.round((duracionRealMin / 60) * 100) / 100;
      const { unidadesHoraReal, eficiencia } = _calcularMetricasProduccion(
        unidadesPorOperario,
        tasaEstandar, // el estándar del proceso es por persona, no por banda
        durHoras,
      );

      await _marcarPedidoIniciado(env, pedido, ahora);

      // UPDATE atómico: reemplaza el LockService consolidado del Apps Script original.
      const reserva = await env.DB.prepare(
        "UPDATE validacion_eanes SET " +
          "unidades_procesadas = unidades_procesadas + ?, " +
          "codigos_sobrantes = codigos_sobrantes + ?, " +
          "estado = CASE WHEN sin_limite = 0 AND (unidades_procesadas + ? + codigos_sobrantes + ? + " +
          "(SELECT COALESCE(SUM(faltante),0) FROM justificaciones WHERE pedido = ? AND ean = ?)) >= unidades_totales THEN 'Completado' ELSE 'En proceso' END, " +
          "actualizado_en = ? " +
          "WHERE numero_pedido = ? AND ean_producto = ? " +
          "AND (sin_limite = 1 OR (unidades_procesadas + ? + codigos_sobrantes + ? + " +
          "(SELECT COALESCE(SUM(faltante),0) FROM justificaciones WHERE pedido = ? AND ean = ?)) <= unidades_totales)",
      )
        .bind(
          total,
          codigosSobrantes,
          total,
          codigosSobrantes,
          pedido,
          codigoEan,
          ahora,
          pedido,
          codigoEan,
          total,
          codigosSobrantes,
          pedido,
          codigoEan,
        )
        .run();

      if (reserva.meta.changes === 0) {
        throw new Error(
          "Otro envío concurrente ya ocupó estas unidades. Verifica el estado actual del pedido antes de reintentar.",
        );
      }
      // A partir de aquí, si algo falla, hay que revertir esta reserva antes de fallar.
      reservaAplicada = { pedido, ean: codigoEan, total, codigosSobrantes };

      // Lista de operarios — informativa, best-effort (ver nota de diseño de la sesión anterior).
      const filaOperarios = await env.DB.prepare(
        "SELECT operarios FROM validacion_eanes WHERE numero_pedido = ? AND ean_producto = ?",
      )
        .bind(pedido, codigoEan)
        .first();
      const listaActual = (filaOperarios.operarios || "")
        .split(" - ")
        .map((s) => s.trim())
        .filter(Boolean);
      operarios.forEach((nom) => {
        if (!listaActual.includes(nom)) listaActual.push(nom);
      });
      await env.DB.prepare(
        "UPDATE validacion_eanes SET operarios = ? WHERE numero_pedido = ? AND ean_producto = ?",
      )
        .bind(listaActual.join(" - "), pedido, codigoEan)
        .run();

      const cedulas = await Promise.all(
        operarios.map((nom) => _buscarCedulaPorNombre(env, nom)),
      );

      const sinCedula = operarios.filter((nom, i) => !cedulas[i]);
      if (sinCedula.length > 0) {
        throw new Error(
          "Estos operarios no tienen cédula en el catálogo: " +
            sinCedula.join(", ") +
            ". Sin cédula el registro no aparece en Control tiempos ni en el " +
            "encadenado de horas. Pide que se corrija la hoja Operarios antes de registrar.",
        );
      }

      const statements = operarios.map((nombre, i) =>
        env.DB.prepare(
          "INSERT INTO registros (lote_id, fecha, coordinador, cedula_operario, operario, supernumerario, cliente, " +
            "proceso, unidades_hora_estandar, sublinea, evento, numero_pedido, ean, unidades_totales_procesadas, " +
            "hora_inicio, hora_final, suplemento, observacion, clasificacion_proceso, duracion_bruta_min, tiempo_total, " +
            "unidades_procesadas_operario, codigos_sobrantes, unidades_hora_real, eficiencia, creado_en) " +
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ).bind(
          loteId,
          fecha,
          coordinador,
          cedulas[i],
          nombre,
          supernumerario,
          datosPedido.cliente,
          procesoValidado.nombre,
          tasaEstandar,
          sublinea,
          datosPedido.evento,
          pedido,
          codigoEan,
          total,
          payload.horaInicio,
          payload.horaFin,
          suplementoMin,
          observaciones,
          datosPedido.clasificacionProceso,
          duracionBrutaMin,
          durHoras,
          unidadesPorOperario,
          codigosSobrantesPorOperario,
          unidadesHoraReal,
          eficiencia,
          ahora,
        ),
      );

      const pedidoCompleto = await _pedidoEstaCompleto(env, pedido);
      if (pedidoCompleto) {
        statements.push(
          env.DB.prepare(
            "INSERT INTO control_produccion (numero_pedido, estado, actualizado_en) VALUES (?, 'Completado', ?) " +
              "ON CONFLICT(numero_pedido) DO UPDATE SET estado = 'Completado', actualizado_en = excluded.actualizado_en",
          ).bind(pedido, ahora),
        );
      }

      const filasParaSheets = operarios.map((nombre, i) => [
        loteId,
        fecha,
        coordinador,
        cedulas[i],
        nombre,
        supernumerario,
        datosPedido.cliente,
        procesoValidado.nombre,
        tasaEstandar,
        sublinea,
        datosPedido.evento,
        pedido,
        codigoEan,
        total,
        payload.horaInicio,
        payload.horaFin,
        suplementoMin,
        observaciones,
        duracionBrutaMin,
        datosPedido.clasificacionProceso,
        durHoras,
        unidadesPorOperario,
        unidadesHoraReal,
        eficiencia,
      ]);

      const resultadosBatch = await env.DB.batch(statements);
      const respuestaFinal = { success: true, loteId };

      // Igual que en el flujo del operario: después del batch y aislado.
      if (pedidoCompleto) {
        try {
          await _actualizarSeguimientoPedido(env, pedido, true);
          ctx.waitUntil(_pushSeguimientoYMarcar(env, pedido));
        } catch (errSeguimiento) {
          // No relanzamos: el registro ya es válido en D1.
        }
      }

      // A partir de aquí D1 ya tiene el estado correcto — cualquier fallo de acá
      // en adelante NO debe liberar la idempotencia ni fallarle al usuario.
      try {
        await _confirmarIdempotencia(env, huella, respuestaFinal);
        const idsRegistros = resultadosBatch
          .slice(0, operarios.length)
          .map((r) => r.meta.last_row_id);
        const filasParaSheetsConId = filasParaSheets.map((fila, i) => [
          ...fila,
          idsRegistros[i],
        ]);
        ctx.waitUntil(
          _pushRegistrosYMarcar(env, idsRegistros, filasParaSheetsConId),
        );
      } catch (errPostCommit) {
        // No relanzamos: el registro ya es válido en D1. Queda sin push
        // inmediato, pero la reconciliación periódica lo recoge.
      }

      return respuestaFinal;
    } catch (err) {
      if (reservaAplicada) {
        await _revertirReserva(env, reservaAplicada);
      }
      await _liberarIdempotencia(env, huella);
      throw err;
    }
  },

  async submitRegistroOperario(payload, env, ctx) {
    const { fecha, hora } = _ahoraBogota();
    // Las horas salían del payload y eran lo que distinguía dos envíos
    // legítimos iguales. Al salir de ahí, la huella necesita otro
    // diferenciador: el idEnvio del formulario, con la hora como respaldo
    // para un navegador con caché vieja.
    // horaCaptura fuera de la huella: si un envío llegó a D1 pero la respuesta
    // se perdió y el operario reconfirma la cédula, el reintento lleva otra
    // captura con el mismo idEnvio y debe devolver la respuesta guardada, no
    // crear un segundo registro.
    const huella = await _huellaPayload("submitRegistroOperario", {
      ...payload,
      horaCaptura: undefined,
      _nonce: payload.idEnvio || hora,
    });
    const chequeo = await _chequearIdempotencia(env, huella);
    if (chequeo.duplicado) return chequeo.respuesta;
    if (chequeo.enProceso) {
      return {
        success: false,
        message:
          "Este registro ya se está procesando (posible doble envío). Espera unos segundos y verifica en Registros antes de reintentar.",
      };
    }

    let reservaAplicada = null;

    try {
      const cedula = String(payload.cedula || "").trim();
      if (!cedula) throw new Error("Falta la cédula del operario.");

      const operarioMatch = await env.DB.prepare(
        "SELECT nombre_operario AS nombre, cedula FROM operarios WHERE cedula = ? AND activo = 1",
      )
        .bind(cedula)
        .first();
      if (!operarioMatch)
        throw new Error(
          'La cédula "' + cedula + '" no existe en el catálogo de Operarios.',
        );

      const { coordinador, supernumerario } =
        await _validarSupervisorYSupernumerario(
          env,
          String(payload.coordinador || "").trim(),
          String(payload.supernumerario || "").trim(),
        );

      const pedido = await _resolverPedido(env, payload.pedido);
      if (!pedido) throw new Error("Falta el número de pedido.");
      let codigoEan = String(payload.codigoEan || "").trim();
      if (!codigoEan)
        throw new Error("Falta escanear el código EAN del producto.");

      const total = Number(payload.unidadesProcesadas);
      if (!Number.isFinite(total) || total < 0)
        throw new Error("Unidades procesadas inválidas.");

      const validacion = await _localizarValidacionEan(env, pedido, codigoEan);
      codigoEan = validacion.ean_producto;
      const unidadesEsperadas = validacion.unidades_totales;
      const sublinea = validacion.sublinea;
      const datosPedido = await _obtenerDatosPedido(env, pedido, sublinea);

      const codigosSobrantes = _validarCodigosSobrantes(
        payload.codigosSobrantes,
      );
      const restantes =
        unidadesEsperadas -
        validacion.unidades_procesadas -
        validacion.justificado -
        validacion.codigos_sobrantes;
      if (!validacion.sin_limite && total + codigosSobrantes > restantes) {
        throw new Error(
          "La cantidad ingresada (" +
            total +
            (codigosSobrantes > 0
              ? " unidades + " + codigosSobrantes + " sobrantes"
              : " unidades") +
            ") no corresponde con lo disponible de este pedido",
        );
      }

      const procesoValidado = await _obtenerProcesoValidado(
        env,
        payload.proceso,
        pedido,
      );
      const tasaEstandar = procesoValidado.unidadesHora;
      const suplementoMin = Number(payload.suplementoMin) || 0;
      // La hora fin es la que dio el servidor al confirmar la cédula
      // (payload.horaCaptura): lo que el operario tarda digitando no cuenta.
      // payload.horaInicio y payload.horaFin se siguen ignorando. Sin captura
      // válida (navegador con caché vieja) se usa la hora actual, como antes.
      const horaCaptura = String(payload.horaCaptura || "").trim();
      const horaFin = /^([01]\d|2[0-3]):[0-5]\d$/.test(horaCaptura)
        ? horaCaptura
        : hora;
      const { inicio: horaInicio, turnoConfigurado } = await _inicioCadena(
        env,
        operarioMatch.cedula,
        fecha,
      );

      if (horaFin > HORA_LIMITE_ENVIO) {
        throw new Error(
          "Fuera del horario de registro (el límite para enviar es las " +
            HORA_LIMITE_ENVIO +
            "). Pide a tu supernumerario que registre por ti.",
        );
      }
      // La cadena puede venir adelantada por un registro del administrador con
      // la hora fin mal digitada: el mensaje tiene que decir qué hacer.
      if (horaFin < horaInicio) {
        throw new Error(
          "Tu hora fin (" +
            horaFin +
            ") quedó antes de tu hora de inicio (" +
            horaInicio +
            "). Vuelve a confirmar tu cédula; si sigue igual, pide a tu supernumerario que corrija tu último registro del día en Editar registros.",
        );
      }
      // Mensaje propio: con horas encadenadas esto lo dispara un operario
      // haciendo lo correcto, no una captura invertida.
      if (horaFin === horaInicio) {
        throw new Error(
          "Tu hora fin quedó igual a tu hora de inicio (" +
            horaInicio +
            "). Espera a que cambie el minuto y vuelve a confirmar tu cédula.",
        );
      }
      // Sin piso de duración: con horas encadenadas el operario no elige la
      // ventana, así que un bloque corto es dato real, no error de captura.
      // Quedan vivas dos guardas: duración bruta 0 y suplemento > duración.
      const { duracionBrutaMin, duracionRealMin } = _calcularDuracionValidada(
        horaInicio,
        horaFin,
        suplementoMin,
      );

      const observaciones = String(payload.observaciones || "").trim();
      const loteId = crypto.randomUUID();
      const ahora = _ahoraISO();
      const durHoras = Math.round((duracionRealMin / 60) * 100) / 100;
      const { unidadesHoraReal, eficiencia } = _calcularMetricasProduccion(
        total,
        tasaEstandar,
        durHoras,
      );

      await _marcarPedidoIniciado(env, pedido, ahora);

      const reserva = await env.DB.prepare(
        "UPDATE validacion_eanes SET " +
          "unidades_procesadas = unidades_procesadas + ?, " +
          "codigos_sobrantes = codigos_sobrantes + ?, " +
          "estado = CASE WHEN sin_limite = 0 AND (unidades_procesadas + ? + codigos_sobrantes + ? + " +
          "(SELECT COALESCE(SUM(faltante),0) FROM justificaciones WHERE pedido = ? AND ean = ?)) >= unidades_totales THEN 'Completado' ELSE 'En proceso' END, " +
          "actualizado_en = ? " +
          "WHERE numero_pedido = ? AND ean_producto = ? " +
          "AND (sin_limite = 1 OR (unidades_procesadas + ? + codigos_sobrantes + ? + " +
          "(SELECT COALESCE(SUM(faltante),0) FROM justificaciones WHERE pedido = ? AND ean = ?)) <= unidades_totales)",
      )
        .bind(
          total,
          codigosSobrantes,
          total,
          codigosSobrantes,
          pedido,
          codigoEan,
          ahora,
          pedido,
          codigoEan,
          total,
          codigosSobrantes,
          pedido,
          codigoEan,
        )
        .run();

      if (reserva.meta.changes === 0) {
        throw new Error(
          "Otro envío concurrente ya ocupó estas unidades. Verifica el estado actual del pedido antes de reintentar.",
        );
      }
      // A partir de aquí, si algo falla, hay que revertir esta reserva antes de fallar.
      reservaAplicada = { pedido, ean: codigoEan, total, codigosSobrantes };

      const filaOperarios = await env.DB.prepare(
        "SELECT operarios FROM validacion_eanes WHERE numero_pedido = ? AND ean_producto = ?",
      )
        .bind(pedido, codigoEan)
        .first();
      const listaActual = (filaOperarios.operarios || "")
        .split(" - ")
        .map((s) => s.trim())
        .filter(Boolean);
      if (!listaActual.includes(operarioMatch.nombre))
        listaActual.push(operarioMatch.nombre);
      await env.DB.prepare(
        "UPDATE validacion_eanes SET operarios = ? WHERE numero_pedido = ? AND ean_producto = ?",
      )
        .bind(listaActual.join(" - "), pedido, codigoEan)
        .run();

      // Guarda atómica de la cadena (P1-1): si otro envío del mismo operario
      // ya ocupó una franja que termina después de esta hora de inicio, el
      // INSERT no escribe nada y devuelve changes = 0. Es '>' estricto porque
      // el predecesor legítimo tiene hora_final exactamente = horaInicio.
      const insercion = await env.DB.prepare(
        "INSERT INTO registros (lote_id, fecha, coordinador, cedula_operario, operario, supernumerario, cliente, " +
          "proceso, unidades_hora_estandar, sublinea, evento, numero_pedido, ean, unidades_totales_procesadas, " +
          "hora_inicio, hora_final, suplemento, observacion, clasificacion_proceso, duracion_bruta_min, tiempo_total, " +
          "unidades_procesadas_operario, codigos_sobrantes, unidades_hora_real, eficiencia, creado_en, hora_inicio_auto) " +
          "SELECT ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1 " +
          "WHERE NOT EXISTS (SELECT 1 FROM registros WHERE cedula_operario = ? AND fecha = ? " +
          "AND anulado = 0 AND hora_final > ?)",
      )
        .bind(
          loteId,
          fecha,
          coordinador,
          operarioMatch.cedula,
          operarioMatch.nombre,
          supernumerario,
          datosPedido.cliente,
          procesoValidado.nombre,
          tasaEstandar,
          sublinea,
          datosPedido.evento,
          pedido,
          codigoEan,
          total,
          horaInicio,
          horaFin,
          suplementoMin,
          observaciones,
          datosPedido.clasificacionProceso,
          duracionBrutaMin,
          durHoras,
          total,
          codigosSobrantes,
          unidadesHoraReal,
          eficiencia,
          ahora,
          operarioMatch.cedula,
          fecha,
          horaInicio,
        )
        .run();

      if (insercion.meta.changes === 0) {
        throw new Error(
          "Otro envío tuyo ocupó esta franja de tiempo justo antes (desde " +
            horaInicio +
            "). Vuelve a confirmar tu cédula para tomar la hora nueva y envía otra vez.",
        );
      }
      const idRegistro = insercion.meta.last_row_id;
      // La fila ya existe: a partir de acá la reserva es legítima y NO debe
      // revertirse. Antes el INSERT iba en el batch y un fallo no dejaba nada
      // escrito; ahora sí. Si algo falla más abajo, el operario reintenta y la
      // guarda de cadena rechaza el duplicado.
      reservaAplicada = null;

      // Fuera del lote a propósito: si el INSERT se rechaza por la guarda de
      // arriba, este estado no debe quedar escrito de más.
      const pedidoCompleto = await _pedidoEstaCompleto(env, pedido);
      if (pedidoCompleto) {
        await env.DB.prepare(
          "INSERT INTO control_produccion (numero_pedido, estado, actualizado_en) VALUES (?, 'Completado', ?) " +
            "ON CONFLICT(numero_pedido) DO UPDATE SET estado = 'Completado', actualizado_en = excluded.actualizado_en",
        )
          .bind(pedido, ahora)
          .run();
      }

      const filaParaSheets = [
        loteId,
        fecha,
        coordinador,
        operarioMatch.cedula,
        operarioMatch.nombre,
        supernumerario,
        datosPedido.cliente,
        procesoValidado.nombre,
        tasaEstandar,
        sublinea,
        datosPedido.evento,
        pedido,
        codigoEan,
        total,
        horaInicio,
        horaFin,
        suplementoMin,
        observaciones,
        duracionBrutaMin,
        datosPedido.clasificacionProceso,
        durHoras,
        total,
        unidadesHoraReal,
        eficiencia,
      ];

      const respuestaFinal = {
        success: true,
        loteId,
        horaInicio,
        horaFin,
        eficiencia,
        turnoConfigurado,
      };

      // La fila se calcula DESPUÉS del INSERT: eficiencia, supernumerarios y
      // coordinadores salen de los registros, este incluido. Aislado en su
      // propio try para que un fallo aquí no se lleve el push de registros.
      if (pedidoCompleto) {
        try {
          await _actualizarSeguimientoPedido(env, pedido, true);
          ctx.waitUntil(_pushSeguimientoYMarcar(env, pedido));
        } catch (errSeguimiento) {
          // No relanzamos: el registro ya es válido en D1.
        }
      }

      try {
        await _confirmarIdempotencia(env, huella, respuestaFinal);
        ctx.waitUntil(
          _pushRegistrosYMarcar(
            env,
            [idRegistro],
            [[...filaParaSheets, idRegistro]],
          ),
        );
      } catch (errPostCommit) {
        // No relanzamos: el registro ya es válido en D1.
      }

      return respuestaFinal;
    } catch (err) {
      if (reservaAplicada) {
        await _revertirReserva(env, reservaAplicada);
      }
      await _liberarIdempotencia(env, huella);
      throw err;
    }
  },

  // Justificación por pedido completo: un solo motivo cierra todos los EAN
  // con faltante. Todo va en un solo batch (D1 lo ejecuta como transacción):
  // si el faltante de algún EAN cambió, su INSERT lleva faltante NULL, choca
  // con el NOT NULL y D1 deshace el batch completo. Cierran todos o ninguno.
  // justificaciones.supernumerario guarda a quien justificó ("Justificado por").
  async justificarPedido(payload, env, ctx) {
    const huella = await _huellaPayload("justificarPedido", payload);
    const chequeo = await _chequearIdempotencia(env, huella);
    if (chequeo.duplicado) return chequeo.respuesta;
    if (chequeo.enProceso) {
      return {
        success: false,
        message:
          "Esta justificación ya se está procesando (posible doble envío). Espera unos segundos y verifica antes de reintentar.",
      };
    }

    try {
      const pedido = await _resolverPedido(env, payload.pedido);
      const motivo = String(payload.motivo || "").trim();
      const justificadoPor = String(payload.justificadoPor || "").trim();
      const faltanteEsperado =
        Math.round(Number(payload.faltanteEsperado) * 100) / 100;

      if (!pedido) throw new Error("Falta el número de pedido.");
      if (!motivo)
        throw new Error("Debes escribir el motivo de la justificación.");
      if (!justificadoPor)
        throw new Error("Escribe el nombre de quien hace la justificación.");
      if (!Number.isFinite(faltanteEsperado) || faltanteEsperado <= 0)
        throw new Error("Falta el faltante del pedido que se va a justificar.");

      // Lectura previa: da los mensajes claros y arma el batch. La autoridad
      // real es la guarda de cada INSERT, que vuelve a leer dentro del batch.
      const { results: eanes } = await env.DB.prepare(
        "SELECT v.ean_producto AS ean, v.sin_limite AS sinLimite, " +
          "(v.unidades_totales - v.unidades_procesadas - v.codigos_sobrantes - " +
          "(SELECT COALESCE(SUM(j.faltante), 0) FROM justificaciones j " +
          "WHERE j.pedido = v.numero_pedido AND j.ean = v.ean_producto)) AS faltante " +
          "FROM validacion_eanes v WHERE v.numero_pedido = ?",
      )
        .bind(pedido)
        .all();

      if (eanes.length === 0)
        throw new Error(
          "El pedido " + pedido + " no existe en Validación eanes.",
        );
      if (eanes.some((e) => e.sinLimite === 1))
        throw new Error(
          "Este pedido tiene EANes sin límite de unidades; no se puede cerrar por justificación.",
        );

      const conFaltante = eanes.filter((e) => e.faltante > 0);
      if (conFaltante.length === 0)
        throw new Error(
          "Este pedido ya no tiene faltante pendiente por justificar.",
        );

      const faltanteActual =
        Math.round(conFaltante.reduce((s, e) => s + e.faltante, 0) * 100) / 100;
      if (faltanteActual !== faltanteEsperado) {
        throw new Error(
          "El faltante del pedido cambió: ahora es " +
            faltanteActual +
            " unidades y se mostraron " +
            faltanteEsperado +
            ". Vuelve a buscar el pedido y revisa antes de cerrar.",
        );
      }

      const ahora = _ahoraISO();
      const hoy = _fechaHoyBogota();

      // sincronizado_sheets = 1: la hoja vieja "Justificaciones" no recibe
      // nada; el dato llega a Sheets por "Seguimiento paquetes".
      const statements = conFaltante.map((e) =>
        env.DB.prepare(
          "INSERT INTO justificaciones (fecha, supernumerario, pedido, ean, faltante, motivo, " +
            "creado_en, sincronizado_sheets, sincronizado_en) VALUES (?, ?, ?, ?, " +
            "(SELECT CASE WHEN (v.unidades_totales - v.unidades_procesadas - v.codigos_sobrantes - " +
            "(SELECT COALESCE(SUM(j.faltante), 0) FROM justificaciones j " +
            "WHERE j.pedido = v.numero_pedido AND j.ean = v.ean_producto)) = ? THEN ? END " +
            "FROM validacion_eanes v WHERE v.numero_pedido = ? AND v.ean_producto = ? AND v.sin_limite = 0), " +
            "?, ?, 1, ?)",
        ).bind(
          hoy,
          justificadoPor,
          pedido,
          e.ean,
          e.faltante,
          e.faltante,
          pedido,
          e.ean,
          motivo,
          ahora,
          ahora,
        ),
      );

      // Un solo UPDATE para todo el pedido, después de los INSERT: cierra
      // cada EAN cuyo faltante quedó en 0 (incluye los que ya estaban en 0
      // sin marcar Completado).
      statements.push(
        env.DB.prepare(
          "UPDATE validacion_eanes SET estado = 'Completado', actualizado_en = ? " +
            "WHERE numero_pedido = ? AND sin_limite = 0 AND COALESCE(estado, '') <> 'Completado' " +
            "AND (unidades_totales - unidades_procesadas - codigos_sobrantes - " +
            "(SELECT COALESCE(SUM(j.faltante), 0) FROM justificaciones j " +
            "WHERE j.pedido = validacion_eanes.numero_pedido " +
            "AND j.ean = validacion_eanes.ean_producto)) <= 0",
        ).bind(ahora, pedido),
      );

      try {
        await env.DB.batch(statements);
      } catch (errBatch) {
        if (String(errBatch.message || "").includes("NOT NULL")) {
          throw new Error(
            "El pedido cambió justo antes de confirmar (alguien registró unidades). No se cerró nada. Vuelve a buscar el pedido y revisa el faltante.",
          );
        }
        throw errBatch;
      }

      // De aquí en adelante D1 ya quedó bien: nada debe fallarle al usuario.
      let pedidoCompleto = true;
      try {
        pedidoCompleto = await _sincronizarEstadoPedido(env, pedido);
        await _actualizarSeguimientoPedido(env, pedido, pedidoCompleto);
        ctx.waitUntil(_pushSeguimientoYMarcar(env, pedido));
      } catch (errSeguimiento) {
        // No relanzamos: el cron de seguimiento recoge la fila.
      }

      const respuestaFinal = {
        success: true,
        pedido,
        unidadesJustificadas: faltanteActual,
        eanesCerrados: conFaltante.length,
        pedidoCompleto,
      };

      try {
        await _confirmarIdempotencia(env, huella, respuestaFinal);
      } catch (errPostCommit) {
        // No relanzamos: la justificación ya quedó en D1.
      }

      return respuestaFinal;
    } catch (err) {
      await _liberarIdempotencia(env, huella);
      throw err;
    }
  },

  async editarRegistro(payload, env, ctx) {
    const id = Number(payload.id);
    if (!Number.isInteger(id) || id <= 0)
      throw new Error("Falta el ID del registro a editar.");

    const motivo = String(payload.motivo || "").trim();
    if (!motivo) throw new Error("Debes indicar el motivo de la edición.");

    const editadoPor = String(payload.editadoPor || "").trim();
    if (!editadoPor) throw new Error("Falta el nombre de quien edita.");

    const actual = await env.DB.prepare(
      "SELECT numero_pedido, ean FROM registros WHERE id = ? AND anulado = 0",
    )
      .bind(id)
      .first();
    if (!actual)
      throw new Error(
        "El registro no existe o ya fue anulado. Actualiza la lista e intenta de nuevo.",
      );

    const pedidoOrigen = actual.numero_pedido;
    const eanOrigen = actual.ean;

    // Pedido y EAN DESTINO: si el payload no los trae, se conserva el par
    // actual. El EAN se canoniza contra Validación, así que el preámbulo
    // UPC-A del escáner no abre un par distinto.
    const pedido = payload.pedido
      ? await _resolverPedido(env, payload.pedido)
      : pedidoOrigen;
    if (!pedido) throw new Error("Falta el número de pedido.");
    const validacionDestino = await _localizarValidacionEan(
      env,
      pedido,
      String(payload.ean || "").trim() || eanOrigen,
    );
    const ean = validacionDestino.ean_producto;
    const seMueve = pedido !== pedidoOrigen || ean !== eanOrigen;
    // Cliente, sublínea, evento y clasificación se derivan del pedido/EAN:
    // si el registro se mueve, tienen que moverse con él.
    const datosPedido = await _obtenerDatosPedido(
      env,
      pedido,
      validacionDestino.sublinea,
    );

    const { coordinador, supernumerario } =
      await _validarSupervisorYSupernumerario(
        env,
        String(payload.coordinador || "").trim(),
        String(payload.supernumerario || "").trim(),
      );

    const procesoValidado = await _obtenerProcesoValidado(
      env,
      payload.proceso,
      pedido,
    );
    const tasaEstandar = procesoValidado.unidadesHora;

    const horaInicio = String(payload.horaInicio || "").trim();
    const horaFin = String(payload.horaFin || "").trim();
    const suplementoMin = Number(payload.suplementoMin) || 0;
    // Sin tope de duración: la edición es la vía correctiva y debe poder tocar
    // cualquier registro, incluidos los bloques largos que genera la cadena.
    const { duracionBrutaMin, duracionRealMin } = _calcularDuracionValidada(
      horaInicio,
      horaFin,
      suplementoMin,
    );
    const durHoras = Math.round((duracionRealMin / 60) * 100) / 100;

    const unidadesNuevas = Number(payload.unidadesProcesadas);
    if (!Number.isFinite(unidadesNuevas) || unidadesNuevas < 0)
      throw new Error("Unidades procesadas inválidas.");
    const codigosSobrantesNuevos = _validarCodigosSobrantes(
      payload.codigosSobrantes,
    );

    const observaciones = String(payload.observaciones || "").trim();

    const fecha = String(payload.fecha || "").trim();
    if (!/^\d{4}-\d{2}-\d{2}$/.test(fecha))
      throw new Error("La fecha del registro no tiene un formato válido.");
    if (fecha > _fechaHoyBogota())
      throw new Error("La fecha del registro no puede ser posterior a hoy.");

    const disponible = await env.DB.prepare(
      "SELECT v.unidades_totales AS totales, v.sin_limite AS sinLimite, " +
        "(SELECT COALESCE(SUM(r.unidades_procesadas_operario + r.codigos_sobrantes), 0) " +
        "FROM registros r WHERE r.numero_pedido = v.numero_pedido AND r.ean = v.ean_producto " +
        "AND r.anulado = 0 AND r.id != ?) AS ocupadoOtras, " +
        "(SELECT COALESCE(SUM(faltante), 0) FROM justificaciones " +
        "WHERE pedido = v.numero_pedido AND ean = v.ean_producto) AS justificado " +
        "FROM validacion_eanes v WHERE v.numero_pedido = ? AND v.ean_producto = ?",
    )
      .bind(id, pedido, ean)
      .first();

    const restante =
      disponible.totales - disponible.ocupadoOtras - disponible.justificado;
    if (
      !disponible.sinLimite &&
      unidadesNuevas + codigosSobrantesNuevos > restante
    ) {
      throw new Error(
        "La cantidad ingresada (" +
          unidadesNuevas +
          (codigosSobrantesNuevos > 0
            ? " unidades + " + codigosSobrantesNuevos + " códigos sobrantes"
            : " unidades") +
          ") supera lo disponible " +
          (seMueve
            ? "del EAN destino " + ean + " del pedido " + pedido
            : "del pedido") +
          " (" +
          restante +
          " de " +
          disponible.totales +
          " totales, contando las demás filas y lo justificado). Verifica antes de continuar.",
      );
    }

    const { unidadesHoraReal, eficiencia } = _calcularMetricasProduccion(
      unidadesNuevas,
      tasaEstandar,
      durHoras,
    );

    const ahora = _ahoraISO();
    const resultado = await env.DB.prepare(
      "UPDATE registros SET fecha = ?, coordinador = ?, supernumerario = ?, proceso = ?, " +
        "numero_pedido = ?, ean = ?, cliente = ?, sublinea = ?, evento = ?, " +
        "clasificacion_proceso = ?, " +
        "unidades_hora_estandar = ?, hora_inicio = ?, hora_final = ?, suplemento = ?, " +
        "observacion = ?, duracion_bruta_min = ?, tiempo_total = ?, " +
        "unidades_procesadas_operario = ?, codigos_sobrantes = ?, unidades_hora_real = ?, " +
        "eficiencia = ?, editado_por = ?, editado_en = ?, motivo_edicion = ?, " +
        "anulado_sincronizado_sheets = 0, " +
        // El CASE compara contra la hora_inicio ANTERIOR: en SQLite el lado
        // derecho de un SET se evalúa sobre la fila original. Si el editor
        // cambió la hora de inicio, la fila deja de ser automática.
        "hora_inicio_auto = CASE WHEN hora_inicio = ? THEN hora_inicio_auto ELSE 0 END " +
        "WHERE id = ? AND anulado = 0",
    )
      .bind(
        fecha,
        coordinador,
        supernumerario,
        procesoValidado.nombre,
        pedido,
        ean,
        datosPedido.cliente,
        validacionDestino.sublinea,
        datosPedido.evento,
        datosPedido.clasificacionProceso,
        tasaEstandar,
        horaInicio,
        horaFin,
        suplementoMin,
        observaciones,
        duracionBrutaMin,
        durHoras,
        unidadesNuevas,
        codigosSobrantesNuevos,
        unidadesHoraReal,
        eficiencia,
        editadoPor,
        ahora,
        motivo,
        horaInicio,
        id,
      )
      .run();

    if (resultado.meta.changes === 0) {
      throw new Error(
        "El registro fue anulado justo antes de guardar los cambios. Actualiza la lista e intenta de nuevo.",
      );
    }

    // Mover unidades = recalcular LOS DOS pares desde la fuente, nunca llevar
    // un delta a mano: el destino las recibe y el origen las suelta.
    await _recalcularContadorValidacion(env, pedido, ean);
    if (seMueve) {
      await _recalcularContadorValidacion(env, pedidoOrigen, eanOrigen);
    }
    const pedidoCompleto = await _sincronizarEstadoPedido(env, pedido);
    await _actualizarSeguimientoPedido(env, pedido, pedidoCompleto);
    // Mover un registro toca DOS pedidos: el destino puede quedar cerrado y el
    // origen puede reabrirse. Cada uno necesita su propia fila actualizada.
    if (pedidoOrigen !== pedido) {
      const origenCompleto = await _sincronizarEstadoPedido(env, pedidoOrigen);
      await _actualizarSeguimientoPedido(env, pedidoOrigen, origenCompleto);
      ctx.waitUntil(_pushSeguimientoYMarcar(env, pedidoOrigen));
    }

    ctx.waitUntil(_pushFilaRegistroYMarcar(env, id));
    ctx.waitUntil(_pushSeguimientoYMarcar(env, pedido));

    return { success: true, id, pedido, ean, movido: seMueve, pedidoCompleto };
  },

  async anularRegistro(payload, env, ctx) {
    const id = Number(payload.id);
    if (!Number.isInteger(id) || id <= 0)
      throw new Error("Falta el ID del registro a anular.");

    const motivo = String(payload.motivo || "").trim();
    if (!motivo) throw new Error("Debes indicar el motivo de la anulación.");

    const anuladoPor = String(payload.anuladoPor || "").trim();
    if (!anuladoPor) throw new Error("Falta el nombre de quien anula.");

    const actual = await env.DB.prepare(
      "SELECT numero_pedido, ean FROM registros WHERE id = ? AND anulado = 0",
    )
      .bind(id)
      .first();
    if (!actual)
      throw new Error(
        "El registro no existe o ya estaba anulado. Actualiza la lista e intenta de nuevo.",
      );

    const pedido = actual.numero_pedido;
    const ean = actual.ean;
    const ahora = _ahoraISO();

    const resultado = await env.DB.prepare(
      "UPDATE registros SET anulado = 1, anulado_por = ?, anulado_en = ?, " +
        "motivo_anulacion = ?, anulado_sincronizado_sheets = 0 WHERE id = ? AND anulado = 0",
    )
      .bind(anuladoPor, ahora, motivo, id)
      .run();

    if (resultado.meta.changes === 0) {
      throw new Error(
        "El registro fue anulado por otro proceso justo antes. Actualiza la lista e intenta de nuevo.",
      );
    }

    await _recalcularContadorValidacion(env, pedido, ean);
    const pedidoCompleto = await _sincronizarEstadoPedido(env, pedido);
    // Si la anulación reabrió el pedido, esto deja la fila como no vigente y
    // el push la borra de la hoja.
    await _actualizarSeguimientoPedido(env, pedido, pedidoCompleto);

    ctx.waitUntil(_pushFilaRegistroYMarcar(env, id));
    ctx.waitUntil(_pushSeguimientoYMarcar(env, pedido));

    return { success: true, id, pedidoCompleto };
  },

  async reactivarRegistro(payload, env, ctx) {
    const id = Number(payload.id);
    if (!Number.isInteger(id) || id <= 0)
      throw new Error("Falta el ID del registro a reactivar.");

    const motivo = String(payload.motivo || "").trim();
    if (!motivo) throw new Error("Debes indicar el motivo de la reactivación.");

    const reactivadoPor = String(payload.reactivadoPor || "").trim();
    if (!reactivadoPor) throw new Error("Falta el nombre de quien reactiva.");

    const actual = await env.DB.prepare(
      "SELECT numero_pedido, ean, unidades_procesadas_operario, codigos_sobrantes " +
        "FROM registros WHERE id = ? AND anulado = 1",
    )
      .bind(id)
      .first();
    if (!actual)
      throw new Error(
        "El registro no existe o no está anulado. Actualiza la lista e intenta de nuevo.",
      );

    const pedido = actual.numero_pedido;
    const ean = actual.ean;

    const disponible = await env.DB.prepare(
      "SELECT v.unidades_totales AS totales, v.sin_limite AS sinLimite, " +
        "(SELECT COALESCE(SUM(r.unidades_procesadas_operario + r.codigos_sobrantes), 0) " +
        "FROM registros r WHERE r.numero_pedido = v.numero_pedido AND r.ean = v.ean_producto " +
        "AND r.anulado = 0) AS ocupadoActivos, " +
        "(SELECT COALESCE(SUM(faltante), 0) FROM justificaciones " +
        "WHERE pedido = v.numero_pedido AND ean = v.ean_producto) AS justificado " +
        "FROM validacion_eanes v WHERE v.numero_pedido = ? AND v.ean_producto = ?",
    )
      .bind(pedido, ean)
      .first();

    const restante =
      disponible.totales - disponible.ocupadoActivos - disponible.justificado;
    const ocupaEstaFila =
      actual.unidades_procesadas_operario + actual.codigos_sobrantes;
    if (!disponible.sinLimite && ocupaEstaFila > restante) {
      throw new Error(
        "No se puede reactivar: este registro ocupa " +
          ocupaEstaFila +
          " unidades y el pedido solo tiene " +
          restante +
          " disponibles ahora mismo (contando lo ya activo y lo justificado). " +
          "Edita las unidades antes de reactivarlo, o revisa el pedido.",
      );
    }

    const ahora = _ahoraISO();
    const resultado = await env.DB.prepare(
      "UPDATE registros SET anulado = 0, editado_por = ?, editado_en = ?, " +
        "motivo_edicion = ?, anulado_sincronizado_sheets = 0 WHERE id = ? AND anulado = 1",
    )
      .bind(reactivadoPor, ahora, motivo, id)
      .run();

    if (resultado.meta.changes === 0) {
      throw new Error(
        "El registro fue modificado por otro proceso justo antes. Actualiza la lista e intenta de nuevo.",
      );
    }

    await _recalcularContadorValidacion(env, pedido, ean);
    const pedidoCompleto = await _sincronizarEstadoPedido(env, pedido);
    await _actualizarSeguimientoPedido(env, pedido, pedidoCompleto);

    ctx.waitUntil(_pushFilaRegistroYMarcar(env, id));
    ctx.waitUntil(_pushSeguimientoYMarcar(env, pedido));

    return { success: true, id, pedidoCompleto };
  },

  async eliminarRegistro(payload, env, ctx) {
    const id = Number(payload.id);
    if (!Number.isInteger(id) || id <= 0)
      throw new Error("Falta el ID del registro a eliminar.");

    const actual = await env.DB.prepare(
      "SELECT numero_pedido, ean, lote_id FROM registros WHERE id = ? AND anulado = 1",
    )
      .bind(id)
      .first();
    if (!actual)
      throw new Error(
        "El registro no existe o no está anulado. Solo se pueden eliminar registros ya anulados.",
      );

    const resultado = await env.DB.prepare(
      "DELETE FROM registros WHERE id = ? AND anulado = 1",
    )
      .bind(id)
      .run();

    if (resultado.meta.changes === 0) {
      throw new Error(
        "El registro fue modificado por otro proceso justo antes. Actualiza la lista e intenta de nuevo.",
      );
    }

    // Borrar una fila anulada no cambia unidades_procesadas/codigos_sobrantes
    // (ya estaba excluida de esa suma), pero sí hace falta recalcular para
    // refrescar "operarios" y el estado — sobre todo en filas anuladas antes
    // de que este recálculo existiera, que quedaron con datos obsoletos.
    await _recalcularContadorValidacion(env, actual.numero_pedido, actual.ean);

    // Se encola ANTES de disparar el push y de forma síncrona: si el push
    // falla, o el Worker muere, la fila queda registrada y la reconciliación
    // de 10 min la recoge. Va después del DELETE a propósito — encolar antes
    // dejaría una orden de borrado viva si el DELETE no llegara a aplicarse.
    await env.DB.prepare(
      "INSERT INTO eliminaciones_pendientes (id_registro, lote_id, creado_en) " +
        "VALUES (?, ?, ?) ON CONFLICT(id_registro) DO UPDATE SET " +
        "lote_id = excluded.lote_id, creado_en = excluded.creado_en, " +
        "intentos = 0, ultimo_error = NULL",
    )
      .bind(id, actual.lote_id || "", _ahoraISO())
      .run();

    ctx.waitUntil(_eliminarFilaRegistroSheets(env, id, actual.lote_id));

    return { success: true, id };
  },
};
