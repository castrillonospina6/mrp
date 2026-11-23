// Acciones de solo lectura — traducción mecánica de code.js a D1.

import {
  _localizarValidacionEan,
  _fechaHoyBogota,
  _ahoraBogota,
  TURNO_POR_DEFECTO,
  _resolverPedido,
  _formasEan,
} from "./d1-negocio.js";

export const accionesLectura = {
  async getSupervisoresData(datos, env) {
    const { results } = await env.DB.prepare(
      "SELECT tipo, nombre FROM supervisores ORDER BY tipo, nombre",
    ).all();
    const coordinadores = results
      .filter((r) => r.tipo === "coordinador")
      .map((r) => r.nombre);
    const supernumerarios = results
      .filter((r) => r.tipo === "supernumerario")
      .map((r) => ({ nombre: r.nombre }));
    return { success: true, data: { coordinadores, supernumerarios } };
  },

  async getProcesos(datos, env) {
    const { results } = await env.DB.prepare(
      "SELECT nombre_proceso AS nombre, unidades_hora_estandar AS unidadesHora, " +
        "numero_pedido AS pedido, link_procesos AS link FROM procesos",
    ).all();
    return { success: true, data: results };
  },

  async getOperariosData(datos, env) {
    // Sin cédula: el catálogo se descarga en cada carga y en cada
    // visibilitychange, y el frontend solo usa nombre y equipo.
    const { results } = await env.DB.prepare(
      "SELECT nombre_operario AS nombre, equipo FROM operarios WHERE activo = 1",
    ).all();
    return { success: true, data: results };
  },

  async buscarOperarioPorCedula(datos, env) {
    const cedula = String(datos.cedula || "").trim();
    if (!cedula) return { success: false, message: "Falta la cédula." };

    const { fecha, hora } = _ahoraBogota();
    const row = await env.DB.prepare(
      "SELECT o.nombre_operario AS nombre, o.equipo, o.turno, " +
        "(SELECT MAX(hora_final) FROM registros " +
        "WHERE cedula_operario = ? AND fecha = ? AND anulado = 0) AS ultimoFin " +
        "FROM operarios o WHERE o.cedula = ? AND o.activo = 1",
    )
      .bind(cedula, fecha, cedula)
      .first();

    if (!row) {
      return {
        success: false,
        message: "Cédula no encontrada en el catálogo de Operarios.",
      };
    }

    // horaInicioPrevista es vista previa (el servidor la recalcula al enviar);
    // horaCaptura es la hora fin que se congela al confirmar la cédula.
    const ultimoFin = String(row.ultimoFin || "").trim();
    const turno = String(row.turno || "").trim();
    return {
      success: true,
      nombre: row.nombre,
      equipo: row.equipo,
      horaInicioPrevista: ultimoFin || turno || TURNO_POR_DEFECTO,
      turnoConfigurado: Boolean(ultimoFin || turno),
      horaCaptura: hora,
    };
  },

  async obtenerDatosValidacion(datos, env) {
    const pedido = await _resolverPedido(env, datos.pedido);
    const ean = String(datos.ean || "").trim();

    let row;
    try {
      row = await _localizarValidacionEan(env, pedido, ean);
    } catch (err) {
      return { success: false, message: err.message };
    }

    return {
      success: true,
      subLinea: row.sublinea,
      unidadesEsperadas: row.sin_limite ? null : row.unidades_totales,
      sinLimite: row.sin_limite === 1,
      restantes: row.sin_limite
        ? null
        : row.unidades_totales -
          row.unidades_procesadas -
          row.justificado -
          row.codigos_sobrantes,
    };
  },

  async obtenerResumenPedido(datos, env) {
    const pedido = String(datos.pedido || "").trim();
    const detalle = await _detalleEanesPedido(env, pedido);

    const eanes = detalle.map((e) => ({
      ean: e.ean,
      esperadas: e.esperadas,
      procesadas: e.procesadas,
      sobrantes: e.sobrantes,
      restantes: e.restanteNeto,
      sinLimite: e.sinLimite,
      justificado: e.justificado,
    }));
    const totalPedido = eanes.reduce((s, e) => s + e.esperadas, 0);
    const procesadoPedido = eanes.reduce(
      (s, e) => s + e.procesadas + e.sobrantes,
      0,
    );
    const restantePedido = eanes.reduce((s, e) => s + e.restantes, 0);

    return {
      success: true,
      totalPedido,
      procesadoPedido,
      restantePedido,
      detalleEanes: eanes,
    };
  },

  async obtenerRegistrosParaEditar(datos, env) {
    const pedido = String(datos.pedido || "").trim();
    const operarioTexto = String(datos.operario || "").trim();
    const fecha = String(datos.fecha || "").trim();
    const ean = String(datos.ean || "").trim();
    const coordinador = String(datos.coordinador || "").trim();
    const supernumerario = String(datos.supernumerario || "").trim();
    const proceso = String(datos.proceso || "").trim();

    // Cualquier filtro basta por sí solo y ninguno depende de otro; lo único
    // que se rechaza es pedir la tabla entera sin ningún criterio.
    if (
      !pedido &&
      !operarioTexto &&
      !fecha &&
      !ean &&
      !coordinador &&
      !supernumerario &&
      !proceso
    )
      return {
        success: false,
        message:
          "Usa al menos un filtro para buscar: pedido, operario, fecha, coordinador, supernumerario, proceso o EAN.",
      };

    // Sin cedula_operario: el frontend nunca la lee y el filtro por operario
    // es por nombre, así que no hace falta exponerla en el response.
    let sql =
      "SELECT id, numero_pedido AS pedido, fecha, coordinador, operario, " +
      "supernumerario, proceso, ean, hora_inicio AS horaInicio, hora_final AS horaFinal, " +
      "suplemento, observacion, unidades_procesadas_operario AS unidadesProcesadas, " +
      "codigos_sobrantes AS codigosSobrantes, anulado, anulado_por AS anuladoPor, " +
      "anulado_en AS anuladoEn, motivo_anulacion AS motivoAnulacion, " +
      "editado_por AS editadoPor, editado_en AS editadoEn, motivo_edicion AS motivoEdicion " +
      "FROM registros WHERE 1 = 1";
    const binds = [];
    if (pedido) {
      sql += " AND numero_pedido = ?";
      binds.push(pedido);
    }
    if (ean) {
      // Las dos formas del EAN: el preamble UPC-A del escáner Zebra antepone
      // un "0", y en D1 puede estar guardada cualquiera de las dos.
      const [eanA, eanB] = _formasEan(ean);
      sql += " AND ean IN (?, ?)";
      binds.push(eanA, eanB);
    }
    if (operarioTexto) {
      sql += " AND " + _sqlContiene("operario");
      binds.push(_normalizarTexto(operarioTexto));
    }
    if (fecha) {
      sql += " AND fecha = ?";
      binds.push(fecha);
    }
    if (coordinador) {
      sql += " AND lower(coordinador) = lower(?)";
      binds.push(coordinador);
    }
    if (supernumerario) {
      sql += " AND lower(supernumerario) = lower(?)";
      binds.push(supernumerario);
    }
    if (proceso) {
      sql += " AND " + _sqlContiene("proceso");
      binds.push(_normalizarTexto(proceso));
    }
    sql += " ORDER BY fecha DESC, hora_inicio ASC, id DESC";
    if (!pedido) sql += " LIMIT 100";

    const { results } = await env.DB.prepare(sql)
      .bind(...binds)
      .all();

    return { success: true, registros: results };
  },

  async obtenerSeguimientoPaquetes(datos, env) {
    const { results } = await env.DB.prepare(
      "SELECT ve.numero_pedido AS pedido, ve.subLinea AS subLinea, " +
        "ve.unidadesTotales AS unidadesTotales, ve.unidadesProcesadas AS unidadesProcesadas, " +
        "ve.codigosSobrantes AS codigosSobrantes, ve.sinLimite AS sinLimite, " +
        "ve.soloSinLimite AS soloSinLimite, ve.eanesAbiertos AS eanesAbiertos, " +
        "COALESCE(js.justificado, 0) AS justificado, " +
        "COALESCE(rg.horasTotales,0) AS horasTotales, " +
        "COALESCE(rg.sumaUnidadesPorTiempo,0) AS sumaUnidadesPorTiempo " +
        "FROM (" +
        "SELECT numero_pedido, GROUP_CONCAT(DISTINCT sublinea) AS subLinea, " +
        "SUM(unidades_totales) AS unidadesTotales, SUM(unidades_procesadas) AS unidadesProcesadas, " +
        "SUM(codigos_sobrantes) AS codigosSobrantes, MAX(sin_limite) AS sinLimite, " +
        "MIN(sin_limite) AS soloSinLimite, " +
        "SUM(CASE WHEN COALESCE(estado, '') <> 'Completado' THEN 1 ELSE 0 END) AS eanesAbiertos " +
        "FROM validacion_eanes GROUP BY numero_pedido" +
        ") ve " +
        "LEFT JOIN (" +
        "SELECT pedido, SUM(faltante) AS justificado FROM justificaciones GROUP BY pedido" +
        ") js ON js.pedido = ve.numero_pedido " +
        "LEFT JOIN (" +
        "SELECT numero_pedido, SUM(tiempo_total) AS horasTotales, " +
        "SUM(tiempo_total * unidades_hora_estandar) AS sumaUnidadesPorTiempo " +
        "FROM registros WHERE anulado = 0 GROUP BY numero_pedido" +
        ") rg ON rg.numero_pedido = ve.numero_pedido " +
        "ORDER BY ve.numero_pedido",
    ).all();

    const paquetes = results.map((r) => {
      const unidadesConSobrantes = r.unidadesProcesadas + r.codigosSobrantes;
      // Cerrado = mismo criterio que _pedidoEstaCompleto: todos los EAN en
      // Completado y ninguno sin límite. Etiquetas iguales a las de la hoja.
      const cerrado = !r.sinLimite && r.eanesAbiertos === 0;
      let estado;
      if (cerrado) estado = r.justificado > 0 ? "No cumple" : "Cumple";
      else if (unidadesConSobrantes > 0) estado = "En proceso";
      else estado = "Pendiente";

      const noIniciado = estado === "Pendiente";
      return {
        pedido: r.pedido,
        subLinea: r.subLinea,
        unidadesTotales: r.unidadesTotales,
        unidadesProcesadas: r.unidadesProcesadas,
        // Cerrado por justificación: lo que faltó, igual que en la hoja.
        unidadesPendientes: r.sinLimite
          ? null
          : estado === "No cumple"
            ? r.justificado
            : r.unidadesTotales - r.unidadesProcesadas,
        cerrado,
        sinLimite: r.sinLimite === 1,
        soloSinLimite: r.soloSinLimite === 1,
        estado,
        horasTotales: noIniciado
          ? null
          : Math.round(r.horasTotales * 100) / 100,
        eficiencia:
          noIniciado || r.sumaUnidadesPorTiempo <= 0
            ? null
            : Math.round(
                (r.unidadesProcesadas / r.sumaUnidadesPorTiempo) * 100,
              ),
      };
    });

    return { success: true, paquetes };
  },

  async obtenerControlTiempos(datos, env) {
    const esAdmin = datos.admin === true;
    const cedula = String(datos.cedula || "").trim();
    if (!cedula && !esAdmin)
      return { success: false, message: "Falta la cédula." };

    const filtro = datos.filtro === "historico" ? "historico" : "fecha";
    const hoy = _fechaHoyBogota();
    let fechaFiltro = hoy;
    if (filtro === "fecha") {
      const valor = String(datos.fecha || "").trim();
      if (valor) {
        if (!/^\d{4}-\d{2}-\d{2}$/.test(valor)) {
          return {
            success: false,
            message: "La fecha no tiene un formato válido.",
          };
        }
        if (valor > hoy) {
          return { success: false, message: "No se permiten fechas futuras." };
        }
        fechaFiltro = valor;
      }
    }

    const pedidoTexto = String(datos.pedido || "").trim();
    const pedido = pedidoTexto ? await _resolverPedido(env, pedidoTexto) : "";

    let sql =
      "SELECT fecha, coordinador, operario, supernumerario, numero_pedido AS pedido, " +
      "cliente, proceso, ean, unidades_procesadas_operario AS unidadesProcesadas, eficiencia, " +
      "suplemento, " +
      "unidades_hora_estandar AS unidadesHoraEstandar, hora_inicio AS horaInicio, " +
      "hora_final AS horaFinal, anulado " +
      "FROM registros WHERE 1 = 1";
    const binds = [];

    if (cedula) {
      sql += " AND cedula_operario = ?";
      binds.push(cedula);
    }
    if (filtro === "fecha") {
      sql += " AND fecha = ?";
      binds.push(fechaFiltro);
    }
    if (pedido) {
      sql += " AND numero_pedido = ?";
      binds.push(pedido);
    }
    // Pedido y proceso los usan las dos vistas; el resto es solo administrador.
    const proceso = String(datos.proceso || "").trim();
    if (proceso) {
      sql += " AND " + _sqlContiene("proceso");
      binds.push(_normalizarTexto(proceso));
    }

    // Filtros exclusivos de la vista administrador.
    if (esAdmin) {
      const coordinador = String(datos.coordinador || "").trim();
      if (coordinador) {
        sql += " AND lower(coordinador) = lower(?)";
        binds.push(coordinador);
      }
      const supernumerario = String(datos.supernumerario || "").trim();
      if (supernumerario) {
        sql += " AND lower(supernumerario) = lower(?)";
        binds.push(supernumerario);
      }
      const operario = String(datos.operario || "").trim();
      if (operario) {
        sql += " AND " + _sqlContiene("operario");
        binds.push(_normalizarTexto(operario));
      }
      const cliente = String(datos.cliente || "").trim();
      if (cliente) {
        sql += " AND " + _sqlContiene("cliente");
        binds.push(_normalizarTexto(cliente));
      }
      // Las dos formas del EAN: el preamble UPC-A del escáner Zebra antepone
      // un "0", y en D1 puede estar guardada cualquiera de las dos.
      const eanTexto = String(datos.ean || "").trim();
      if (eanTexto) {
        const [eanA, eanB] = _formasEan(eanTexto);
        sql += " AND ean IN (?, ?)";
        binds.push(eanA, eanB);
      }
    }

    sql += " ORDER BY fecha DESC, hora_inicio DESC, id DESC";
    // Sin tope en administrador: la vista del operario conserva el LIMIT 100.
    if (!esAdmin) sql += " LIMIT 100";

    const { results } = await env.DB.prepare(sql)
      .bind(...binds)
      .all();

    const registros = results.map((r) => ({
      fecha: r.fecha,
      coordinador: r.coordinador,
      operario: r.operario,
      supernumerario: r.supernumerario,
      pedido: r.pedido,
      cliente: r.cliente,
      proceso: r.proceso,
      ean: r.ean,
      unidadesProcesadas: r.unidadesProcesadas,
      suplemento: r.suplemento || 0,
      eficiencia: r.unidadesHoraEstandar > 0 ? r.eficiencia : null,
      horaInicio: r.horaInicio,
      horaFinal: r.horaFinal,
      anulado: !!r.anulado,
    }));

    return { success: true, registros };
  },
};

// Equivalente a _obtenerDetalleEanesPedido de code.js — una consulta SQL en vez de recorrer la hoja.
async function _detalleEanesPedido(env, pedido) {
  const { results } = await env.DB.prepare(
    "SELECT ean_producto AS ean, unidades_totales AS esperadas, unidades_procesadas AS procesadas, " +
      "codigos_sobrantes AS sobrantes, estado, sin_limite AS sinLimite, " +
      "(SELECT COALESCE(SUM(faltante),0) FROM justificaciones j " +
      " WHERE j.pedido = v.numero_pedido AND j.ean = v.ean_producto) AS justificado " +
      "FROM validacion_eanes v WHERE numero_pedido = ?",
  )
    .bind(pedido)
    .all();

  return results.map((r) => ({
    ean: r.ean,
    esperadas: r.esperadas,
    procesadas: r.procesadas,
    sobrantes: r.sobrantes,
    justificado: r.justificado,
    restanteNeto: r.sinLimite
      ? null
      : r.esperadas - r.procesadas - r.justificado - r.sobrantes,
    sinLimite: r.sinLimite === 1,
    estado: r.estado || "Pendiente",
  }));
}

// Normaliza texto para comparar nombres sin distinguir mayúsculas ni tildes.
function _normalizarTexto(str) {
  return String(str || "")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase();
}

// Genera una expresión SQL que quita tildes (mayús/minús) antes de aplicar LOWER,
// para que LOWER() no dependa de que SQLite maneje bien Unicode.
function _sqlSinTildes(columna) {
  const mapa = [
    ["Á", "a"],
    ["É", "e"],
    ["Í", "i"],
    ["Ó", "o"],
    ["Ú", "u"],
    ["Ñ", "n"],
    ["Ü", "u"],
    ["á", "a"],
    ["é", "e"],
    ["í", "i"],
    ["ó", "o"],
    ["ú", "u"],
    ["ñ", "n"],
    ["ü", "u"],
  ];
  let expr = columna;
  for (const [conTilde, sinTilde] of mapa) {
    expr = `REPLACE(${expr}, '${conTilde}', '${sinTilde}')`;
  }
  return `LOWER(${expr})`;
}

// D1 limita los patrones LIKE a 50 bytes: un nombre de proceso largo con
// los dos "%" lo supera y la consulta falla. instr() no tiene ese tope y
// tampoco interpreta "%" ni "_" dentro del texto buscado.
function _sqlContiene(columna) {
  return "instr(" + _sqlSinTildes(columna) + ", ?) > 0";
}
