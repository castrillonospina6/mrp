# MES modular · Planeación de producción

Sistema web de planeación y ejecución de producción (MES) en Python/Django, construido
alrededor de un **núcleo pequeño + plugins**. Pensado para operación logística
(picking, acondicionado) y planta de alimentos (deshuese), y para crecer de a poco.

## Arranque rápido (Windows / PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py createsuperuser
.\.venv\Scripts\python.exe manage.py seed_demo        # opcional: datos de ejemplo
.\.venv\Scripts\python.exe manage.py runserver
```

Abrir http://127.0.0.1:8508 e iniciar sesión con el superusuario. El sistema usa el **puerto 8508**:
`manage.py runserver` sin puerto ya arranca ahí (otro puerto: variable `MES_PORT` o `runserver 0.0.0.0:NNNN`).

Pruebas: `.\.venv\Scripts\python.exe manage.py test mes_core plugins`

### Arrancar el MES con las tablets de planta

```powershell
.\iniciar.ps1        # o doble clic en iniciar.bat
```

Arranca el MES en la red local (`0.0.0.0:8508`, sin modo depuración) y con él la app de control de piso. Imprime la
dirección para las tablets (`http://IP-DEL-PC:8508/p/piso/`). Usa el `.venv` si funciona y, si no, el Python del
sistema. `-AlIniciarWindows` crea un acceso directo para que arranque solo al iniciar sesión en el PC; `-Port` y
`-Depurar` como en el túnel. La primera vez Windows pide permiso de red para Python: acepta en redes privadas.

### Publicar por serveo (`https://plan-extiblu.serveousercontent.com`)

```powershell
.\serveo.ps1         # o doble clic en serveo.bat
```

Arranca Django en `127.0.0.1:8508` sin modo depuración, agrega la dirección de serveo a `MES_ALLOWED_HOSTS` en `.env`
y abre `ssh -R plan-extiblu:80:127.0.0.1:8508 serveo.net`, reconectando solo si el túnel se cae. Usa las llaves
`~\.ssh\id_serveo_*` (o `-Llave`); el subdominio fijo debe estar registrado con esa llave en serveo. Necesita salida
a `serveo.net` por el puerto 22: si la red lo bloquea, el script lo avisa. `-Subdominio` cambia el nombre.

### Publicar por un túnel de Cloudflare

Con `cloudflared.exe` en la carpeta del proyecto (o en el PATH):

```powershell
.\tunel.ps1          # o doble clic en tunel.bat
```

Arranca Django **sin modo depuración** (los errores no muestran código ni configuración), abre el túnel
e imprime la URL `https://<algo>.trycloudflare.com`. Ctrl+C detiene ambos. La primera vez crea una
clave secreta propia en `.env` (no se sube a git). `-Port 8001` usa otro puerto; `-Depurar` muestra
errores detallados (solo para diagnosticar, no para compartir).

- La URL del túnel rápido cambia en cada arranque; `*.trycloudflare.com` ya está permitido.
- Para un dominio fijo (túnel con nombre), agrégalo en `.env`: `MES_ALLOWED_HOSTS=mes.miempresa.com`.
- Es el servidor de desarrollo de Django: sirve para demos y uso interno, no para producción.

## Estructura

```
config/            Configuración Django (settings, urls)
mes_core/          Núcleo
  models.py        Planta, Centro de trabajo, Producto, Orden, Operación, Reporte de producción
  plugins.py       API de plugins (MESPlugin, ProcessType, OperationTemplate, registro)
  plugin_loader.py Descubrimiento de plugins (carpeta plugins/ + entry points)
  services.py      Casos de uso: crear / liberar / cancelar orden, reportar producción
  planning.py      Planificador de capacidad finita
  signals.py       Eventos para plugins
plugins/
  picking/         Picking de pedidos (líneas por ubicación)
  copacking/       Acondicionado: etiquetas, ganchos, bolsas, cajas → insumos por orden
  food/            Deshuese: rendimiento estándar vs. real y trazabilidad de lotes
templates/         Plantilla base
```

## Conceptos

| Concepto | Qué es |
|---|---|
| **Planta / sede** | CEDI o planta de alimentos. |
| **Centro de trabajo** | Zona, mesa o línea. Su `tipo` (p. ej. `etiquetado`) lo enlaza con las rutas. `capacidad` = estaciones en paralelo. Tiene jornada (inicio–fin). |
| **Tipo de proceso** | Ruta estándar de operaciones que aporta un plugin (picking, acondicionado, deshuese). |
| **Orden** | Producto × cantidad × tipo de proceso. Al crearla se generan sus operaciones. Estados: borrador → planificada → liberada → en proceso → terminada. |
| **Plan** | `Generar plan` programa todas las órdenes abiertas de una planta según prioridad y fecha compromiso, respetando capacidad y jornada. Marca en rojo las que no llegan. |
| **Reporte de producción** | Cantidad buena / merma por operación. Inmutable (trazabilidad). |

## Crear un plugin

1. Crear `plugins/mi_plugin/` con `__init__.py`, `apps.py` y una carpeta `migrations/` con su `__init__.py`.
2. En `apps.py`:

```python
from mes_core.plugins import MESPlugin, ProcessType, OperationTemplate, MenuItem

class MiPluginConfig(MESPlugin):
    name = "plugins.mi_plugin"
    label = "mi_plugin"
    verbose_name = "Mi plugin"
    plugin_version = "0.1.0"
    requires = ()                      # p. ej. ("food",) si depende de otro plugin

    def get_process_types(self):
        return [ProcessType(
            key="marinado", label="Marinado",
            operations=(
                OperationTemplate("Inyección", "inyeccion", minutes_per_unit=0.2, setup_minutes=10),
                OperationTemplate("Reposo", "frio", setup_minutes=240),
            ),
        )]

    def get_menu_items(self):          # opcional
        return [MenuItem("Mi pantalla", "mi_plugin:index")]

    def ready(self):                   # opcional: reaccionar a eventos
        from mes_core.signals import order_completed
        order_completed.connect(lambda sender, order, **kw: ...)
```

3. Opcional: `models.py`, `admin.py`, `urls.py` (se monta solo en `/p/mi_plugin/`), plantillas.
4. `python manage.py makemigrations mi_plugin` y `migrate`. El plugin se descubre solo.

Puntos de extensión disponibles: tipos de proceso, menú, acciones en el detalle de la orden
(`get_order_actions`), inlines en el admin de la orden (`get_order_admin_inlines`), URLs propias
y señales (`order_created`, `order_released`, `order_completed`, `production_reported`, `plan_generated`).

Un plugin también puede distribuirse como paquete pip declarando el entry point `mes.plugins`.

### Activar / desactivar plugins

Variables de entorno (listas separadas por coma):

- `MES_PLUGINS=picking,food` → solo carga esos.
- `MES_DISABLED_PLUGINS=copacking` → carga todos menos esos.

## Configuración por entorno

| Variable | Defecto |
|---|---|
| `MES_SECRET_KEY` | clave insegura de desarrollo (**cambiar en producción**) |
| `MES_DEBUG` | `1` |
| `MES_ALLOWED_HOSTS` | `localhost,127.0.0.1` |
| `MES_TIME_ZONE` | `America/Bogota` |
| `MES_DB_ENGINE` | SQLite; `postgresql` usa `MES_DB_NAME/USER/PASSWORD/HOST/PORT` (requiere `psycopg`) |

## Flujo ventas → planeación → programación

1. **Turnos** (`/configuracion/turnos/`): hora de inicio, duración ↔ hora de fin, número de turnos,
   desayuno, almuerzo y operarios disponibles por turno. Los descansos se descuentan del tiempo hábil
   en todo el sistema (planeación, programador y plan automático).
   - **Desayunos y almuerzos escalonados**: cada descanso admite varios horarios (grupos), p. ej. desayuno
     08:00, 08:20 y 08:40 de 20 min. «Generar» arma N grupos seguidos. Cada persona toma un solo grupo y la
     línea no para: a cada operario se le descuenta el tiempo de un grupo (internamente se ubica en el primer
     grupo, así la capacidad del turno es la real).
   - **Turnos de traslape** (sección propia): turnos que se cruzan con los demás, p. ej. 11:00–19:00, con su
     propio horario, operarios y descansos (pueden no tener desayuno). «Guardar turnos» no los modifica. En las
     horas en que se cruzan: la gente de ambos turnos se suma en la bolsa de operarios; el tablero de capacidad
     reparte la carga entre los dos según su gente (no la cuenta dos veces); y en planeación las horas por día
     son la unión de los turnos (un traslape aporta solo lo que no cubre otro turno, como el almuerzo de la
     mañana). «Completar en 1 turno» y el tope de operarios se miden con los turnos regulares.
   - **Tipos de servicio** (`/configuracion/tipos-de-servicio/`, grupos *Planeación* y *Jefe de planta*): catálogo
     de lo que sale en «Nuevo pedido». Los que aportan los módulos (con ruta estándar de etapas) y los creados a mano.
     Cada uno se activa o desactiva con un interruptor, se renombra, y los manuales sin pedidos se pueden borrar.
     Desactivar no afecta los pedidos que ya lo usan.
2. **Ventas** (`/ordenes/nueva/`, grupo *Ventas*): cliente, producto, tipo de servicio (lista desplegable que también
   acepta texto libre: si no existe se agrega al catálogo; o en blanco), unidades,
   fecha de recepción, fecha de entrega en el cliente y días de tránsito. El sistema calcula
   **despacho = entrega − tránsito**; la producción debe terminar al inicio de la jornada de ese día
   (fecha compromiso). El pedido queda **Por planear**, sin etapas.
3. **Planeación** (`/planeacion/`, grupo *Planeación*): por cada etapa se elige línea, tiempo estándar
   (min/unidad, unidades/min o unidades/hora), alistamiento, **cómo planear** (1 turno, N días,
   N operarios o antes del compromiso) y **cantidad** (todo, parcial —el saldo queda en el backlog— o
   por lotes, que fluyen lote a lote a la etapa siguiente). El sistema calcula en vivo:

   | Resultado | Fórmula |
   |---|---|
   | Horas de trabajo W | min/unidad × unidades ÷ 60 |
   | Operarios n | 1 turno: ⌈W ÷ (h hábiles del turno − alistamiento)⌉ · N días: ⌈W ÷ (N × turnos/día × h hábiles − alistamiento)⌉ · compromiso: reparte las horas hábiles que quedan |
   | Duración (h) | alistamiento + W ÷ n |
   | Horas-hombre | W + alistamiento × n |
   | Turnos / días | duración ÷ h hábiles por turno ÷ turnos por día |

   Avisa si faltan operarios o si no se llega al compromiso. **Guardar y programar** crea las
   operaciones (una por lote) y las ubica en el programador respetando turnos, descansos y mantenimientos.
   **Doble enfoque**: arriba, «¿Cómo quieres planear?» → *Calcular operarios* (das el plazo → calcula
   personas) o *Calcular tiempo* (das las personas → calcula horas, turnos, días y hora de fin). Con
   *Calcular tiempo* aparece «Operarios para todas las etapas» y «Usar libres». Cada etapa puede
   cambiarse sola con sus opciones *Operarios / Tiempo*.
4. **Bolsa de operarios**: el total de la planta se configura en Turnos (un turno con número propio lo
   reemplaza). Cada operación programada ocupa su cuadrilla de inicio a fin y la libera al terminar.
   Planeación muestra por etapa «libres al iniciar → quedan → se liberan a las HH:MM» y la lista de
   próximas liberaciones; si no alcanzan, la etapa espera a que se liberen (desactivable).
5. **Programador**: fila «Operarios» con libres/disponibles por turno, y borde rojo «operarios» cuando
   las cuadrillas simultáneas superan lo disponible.

Roles: los grupos *Ventas* y *Planeación* se crean solos con `migrate`; asigna usuarios en
Administración → Usuarios. El superusuario ve todo.

## Interfaz

- **Menú lateral** a la izquierda, agrupado por Operación, Pedidos, Planeación, Módulos y Configuración. Solo
  muestra lo que el rol del usuario puede usar. En escritorio se contrae a iconos (botón junto al logo; se
  recuerda) y en tableta/celular se abre como cajón con el botón ☰.
- **Tema** claro, oscuro o automático (botón en el pie del menú). Paleta sobria: grises neutros y un solo color
  de marca; verde/ámbar/rojo solo indican estado.
- **Bodegas: botones en vez de lista desplegable** (`{% site_chips %}`, `mes_core/site_scope.py`). Donde tiene
  sentido hay un botón **Todas**: Capacidad de planta (toda la capacidad instalada: cada bodega en su turno, la suma,
  y la carga de todas las líneas), Pedidos, Pedidos en riesgo, Mi turno, Asignar supervisores, Mercar, Insumos,
  Validación EAN y Horas extra. La elección se recuerda en la sesión al pasar de una pantalla a otra. Las pantallas
  que son de una sola bodega (turnos, calendario, reglas, simulación, versiones, traslados, programador) muestran
  solo las bodegas y abren en la última elegida o, si no hay, en la que tiene más líneas.
- **Inicio de sesión** propio en `/acceder/` (cerrar sesión en el pie del menú).
- Estilos centralizados en `mes_core/static/mes_core/app.css` (variables de color, tarjetas, tablas, botones).

### Gantt (`/plan/`)

Escala de tiempo real por **Día / Semana / Mes** con zoom, **por línea** o **por pedido**, búsqueda, tiempo
fuera de turno rayado, mantenimientos, línea de «Ahora» y marca de compromiso por pedido. Con permiso de
planeación: **arrastrar** una barra la mueve (también a otra línea compatible), **estirar** su borde cambia el
fin, y el **clic** abre un panel con el detalle, edición exacta de línea/inicio/fin y «Devolver al backlog».
Usa los mismos datos y validaciones que el programador, así que ambas vistas siempre coinciden.

## Calendario laboral y planeación avanzada

**Calendario** (lo usan programador, Gantt, plan automático, planeación por etapas, capacidad y tablero de planta):

| Función | Dónde | Qué hace |
|---|---|---|
| Días laborables por turno | Turnos y personal → «Días laborables» | L-M-X-J-V-S-D por turno; los días sin marcar no se programan. |
| Festivos | Calendario laboral | Botón «Cargar festivos de Colombia» (Ley Emiliani y Semana Santa calculadas) o manuales; la planta no trabaja. |
| Horas extra | Calendario laboral | Extiende un turno una fecha (desde/hasta, operarios, líneas). |
| Turno adicional | Calendario laboral | Franja que normalmente no existe (p. ej. domingo 06–14 con 40 personas). |
| Cierre | Calendario laboral | Un turno o todo el día no se trabaja, en todas o algunas líneas. |

**Planeación** (menú «Decisiones», «Control» y «Reglas de planeación»):

| Función | Qué hace |
|---|---|
| Reglas de prioridad | Puntaje 0–100 con pesos ajustables: compromiso, nivel del cliente, antigüedad y prioridad. Ordena el backlog y el plan automático. |
| Plan congelado | Las próximas N horas no las mueve el plan automático; moverlas a mano pide confirmación. |
| Tiempos de cambio | Tabla de familia → familia por tipo de línea (más un valor por defecto). El plan los suma y agrupa familias. |
| Dividir operaciones | Manual (panel del programador o del Gantt: «Dividir entre líneas» / «Unir partes») o automática si dura más de X horas. Las partes corren en paralelo. |
| Simulación «¿qué pasa si…?» | Hasta 3 escenarios (pedido nuevo, línea parada, horas extra, turno adicional, día no laborable, prioridad) comparados contra el plan base. «Aplicar» los crea de verdad. |
| Promesa de entrega | En «Nuevo pedido», en vivo: fecha más temprana de entrega con la capacidad real y si cumple la fecha pedida. |
| Horas extra sugeridas | Propone horas extra en turnos sobrecargados y turnos adicionales en días sin trabajo, simulando cuántos atrasos resuelve cada una. |
| Mediano plazo (4–8 semanas) | Horas-hombre y horas-línea disponibles frente a programado + backlog + pronóstico, con recomendaciones (temporales, sábados). |
| Pronóstico de demanda | Por tipo de proceso: misma semana del año pasado × tendencia (con más de un año de historia) o promedio de 8 semanas. |
| Versiones del plan | Foto del plan; comparación entre versiones o contra el plan actual (movidas, cambios de línea, nuevas, retiradas, fin de cada pedido). |
| Aprobación | Planeación propone; el rol **Jefe de planta** aprueba (libera los pedidos a planta) o rechaza con motivo. Opcional: «liberar solo con plan aprobado». |
| Plan vs. real | Por día o turno: adherencia %, planeado frente a reportado por línea y operación, retraso al iniciar y producción fuera del plan. |

## Tablero principal: capacidad de planta (`/`)

Para planeación, por turno y día (selector de planta, día y turno; «Ahora» vuelve al turno en curso):

| Indicador | Cálculo |
|---|---|
| **Personal disponible** | programados − ausencias (programados = número del turno, o total de la planta, o el que se registre ese día) |
| **Ausencias** | se registran en la tarjeta («Registrar ausencias», grupo *Planeación*) |
| **Capacidad disponible** | personal disponible × horas hábiles del turno (sin desayuno ni almuerzo) |
| **Horas requeridas** | Σ cuadrilla × horas hábiles de cada operación programada dentro del turno |
| **Capacidad ocupada** | requeridas ÷ disponible → *Sobrecarga* u *Holgura* en horas |
| **Mercancía / Insumos** | % por orden (se edita en la tabla; «Todo listo» = 100 %), ponderado por las horas de cada orden en el turno |

Además: **próximos 7 días** (ocupación por turno: verde < 85 %, ámbar 85–100 %, rojo > 100 %; clic para ver
ese turno), **carga por línea** (cuello de botella), **Para decidir** (operarios que faltan, cuánto recupera
cubrir ausencias y turnos con holgura a donde mover carga), **pedidos** (por planear, operaciones sin programar,
atrasados, en riesgo, cumplimiento a tiempo 30 días) y **producción de hoy** (buenas, merma, calidad).

## Tablero de planta (`/planta/`)

Pantalla en vivo (se actualiza cada 30 s, con *Pantalla completa* para un televisor en planta). Una tarjeta
por línea con su estado en este momento. Con **Todas** (botones de bodega, como en las demás pantallas) se ven las
líneas de todas las bodegas agrupadas por bodega, cada grupo con su turno; los conteos, paros, estaciones y operarios
se suman.

| Color | Estado | Significa |
|---|---|---|
| Verde | **Rodando** | Hay una operación en proceso (ya arrancó). |
| Verde claro, borde punteado | **Asignada · por iniciar** | Hay una operación programada para ahora que aún no arranca. |
| Morado | **Mantenimiento** | Paro o mantenimiento activo. |
| Gris rayado | **Por asignar** | Línea libre: muestra la siguiente orden y cuántas operaciones de su tipo esperan en el backlog. |
| Gris liso | **Fuera de turno** | La línea no trabaja en el turno actual. |

Cada tarjeta trae orden, producto, cliente, avance (unidades reportadas), operarios y los indicadores del
turno: **Disp.** (tiempo hábil sin paros), **Rend.** (unidades reportadas ÷ esperadas al ritmo estándar) y
**Cal.** (buenas ÷ buenas + merma). Arriba: turno actual, minutos de paro del turno, estaciones y operarios en
uso / libres. Los chips de la leyenda filtran por estado.

## Programador visual (`/programador/`)

Calendario por línea × día × turno (vistas semanal, diaria y mensual):

- **Turnos** por planta (Administración → Plantas). Un turno que cruza medianoche
  (Noche 22:00–06:00) pertenece al día en que termina. Cada línea trabaja los turnos que
  se le asignen (vacío = todos los de la planta).
- **Arrastrar y soltar** desde el backlog o entre celdas/líneas compatibles; estirar por el borde
  derecho; soltar en el backlog o × para desprogramar.
- **Clic en una tarjeta** (del tablero o del backlog) abre el panel de edición: inicio y fin exactos
  con indicador de disponibilidad, línea, turno de inicio/fin, fecha compromiso y prioridad de la orden,
  ruta de la orden, *Abrir orden*, *Guardar/Programar* y *Desprogramar*.
- **Conflictos** (borde rojo): solape de capacidad, secuencia dentro de la orden o mantenimiento.
- **Mantenimientos** por línea, **clientes con nivel** (Bronce/Plata/Oro/VIP), alerta de órdenes atrasadas,
  bitácora de **Cambios**, **Plan automático** (respeta lo programado a mano) e **Importar CSV**
  (`planta, sku, proceso, cantidad, compromiso, prioridad, referencia, cliente`; todo o nada).

## Productos e insumos (catálogos separados)

- **Producto**: lo que se procesa en las órdenes de producción. **Insumo**: lo que se consume para procesarlo
  (etiquetas, bolsas, cajas…). Nunca se mezclan.
- Un pedido (nuevo, cargado desde Excel o por CSV) solo busca y crea **productos**. El inventario, la carga de inventario,
  las listas de materiales (como componente) y las fichas de acondicionado (como material) solo usan **insumos**.
- Si un nombre coincide con uno del otro tipo, se crea uno aparte. Si se escribe el **código** de uno del otro tipo, se
  rechaza con un mensaje claro (los códigos son únicos en todo el catálogo).
- En Administración hay dos listas: **Productos** e **Insumos (catálogo)**. Si alguno quedó mal clasificado, la acción
  «Es un insumo / Es un producto» lo cambia, siempre que aún no se use como el tipo actual.

## Insumos y liberación a planta

Flujo por roles: **Ventas** registra el pedido → **Planeación** planea y programa → **«Enviar a insumos»** (estado
*En insumos*) → en paralelo **Insumos** confirma, entrega y **libera sus insumos** (o devuelve el pedido a planeación
con motivo) y **Logística** lo **merca** → cuando las dos partes liberaron, el pedido **pasa solo a planta** (*Liberada*)
→ **Supervisor** ejecuta (cuadrilla y reportes de producción). Planeación ya no libera directamente; si la planta exige
plan aprobado, aprobar la versión envía los pedidos a insumos.

**Liberación automática a planta** (Planeación → Reglas de planeación, por planta; encendida por defecto): al entregar
el 100 % de las líneas, los insumos del pedido se liberan solos, sin pulsar «Liberar». Apagada, insumos libera a mano.
En ambos casos el pedido espera a logística (pedido mercado); quien termine de último dispara el paso a planta. Un pedido
sin líneas de insumos se libera a mano. Si insumos devuelve el pedido a planeación, al reenviarlo debe liberar de nuevo.

| Pantalla (menú Insumos) | Qué hace |
|---|---|
| **Liberar a planta** (`/insumos/`) | Pedidos por liberar, *esperando logística* (insumos ya liberados), en planta y próximos 14 días (para alistar), con % de insumos entregado y estado de la mercancía. |
| **Insumos del pedido** (`/insumos/pedido/<id>/`) | Líneas: insumo, etapa (o todo el pedido), requerido, existencia en bodega, entregado, «entregar ahora» y a qué supervisor. «Registrar entrega» descuenta inventario; «Entregar y liberar a planta» además pasa el pedido a *Liberada*. Con insumos incompletos exige marcarlo y explicar por qué. «Devolver a planeación» con motivo. Si el producto tiene ficha de acondicionado, las líneas llegan sugeridas. |
| **Inventario** (`/insumos/inventario/`) | Existencia, comprometido (requerido sin entregar en pedidos abiertos), disponible, stock de seguridad y semáforo (OK / bajo stock de seguridad / no alcanza / agotado). Nuevo insumo, entrada, ajuste por conteo, edición y **carga desde Excel** (plantilla descargable). |
| **Movimientos** (`/insumos/movimientos/`) | Entradas, entregas a pedidos y ajustes; no se editan (trazabilidad). |

Reglas: no se entrega más de lo que hay en bodega ni más de lo que falta en la línea; una línea con entregas no se
borra; el % de insumos del pedido (tablero de capacidad) lo calculan las entregas y solo insumos lo modifica a mano.
Reportar producción queda para Supervisor, Jefe de planta y Planeación, y solo con el pedido liberado.

## Automatizaciones

| Qué | Dónde | Cómo funciona |
|---|---|---|
| **Lista de materiales** | Insumos → Listas de materiales (`/insumos/listas/`) | Por producto: insumo, cantidad por unidad, desperdicio % y etapa opcional. Al enviar el pedido a insumos, sus líneas salen calculadas (cantidad × por unidad + desperdicio; unidades y cajas hacia arriba) y ligadas a la etapa. Edición por producto o carga desde Excel (cada producto del archivo queda solo con sus líneas). En «Insumos del pedido», «Traer de la lista de materiales» agrega lo que falte. Sin lista, se usa la ficha del módulo (acondicionado). |
| **Supervisor por línea y turno** | Personal → Supervisor por línea (`/supervision/reglas/`) | Cuadrícula línea × turno. Toda operación programada en esa línea y turno queda con ese supervisor (también al mover la operación o con el plan automático). Lo elegido a mano en «Asignar supervisores» manda: la regla ya no lo cambia. |
| **Bandeja de avisos** | Campana arriba a la derecha (`/avisos/`) | Pedido nuevo → planeación; enviado a insumos → insumos y logística; liberado a planta → supervisores de sus operaciones (o planeación si no tiene); insumo que cruza el stock de seguridad → insumos; asistencia sin confirmar al vencer el plazo → supervisor y jefe de planta; pedido en riesgo → planeación y supervisor; sugerencia de nivel → supervisor. Cada aviso sale una sola vez. Correo opcional (abajo). |
| **Alertas de atraso** | Operación → Pedidos en riesgo (`/control/riesgo/`) y detalle del pedido | Compara lo reportado con lo que debía llevar a esa hora en **horas hábiles**. Si va más de la tolerancia (10 % por defecto) por debajo: «En riesgo» o «Atrasada», con sugerencias (cuántas personas más, cuántas horas más o reprogramar). |
| **Carga de pedidos desde Excel** | Pedidos → Cargar pedidos (`/ordenes/cargar/`) | Ventas sube una fila por pedido (producto, cantidad y fecha de entrega obligatorios). Cliente, producto y tipo de servicio se reconocen sin importar mayúsculas, tildes o espacios; si no existen se crean. Una OP repetida se rechaza. Todo o nada; planeación recibe un solo aviso. Acepta 1.200 (miles con punto). |
| **Sugerencias de nivel** | Personal → Sugerencias de nivel | Horas hábiles de cada operario por competencia en cuadrillas de operaciones con producción reportada (último año). Desde 8 h → novato, 40 h → intermedio, 120 h → experto (`MES_SKILL_HOURS` en settings). El supervisor aprueba (cambia la matriz) o descarta (no se vuelve a proponer). |

Parámetros en Planeación → Reglas de planeación: **tolerancia de atraso (%)** y **plazo para confirmar asistencia**
(minutos después del inicio del turno; 60 = turno de 6:00 → aviso a las 7:00).

Las revisiones por tiempo (asistencia, atrasos y polivalencia) corren solas cada 5 minutos como máximo mientras alguien
usa la aplicación. Para que corran aunque nadie entre, programa `python manage.py mes_avisos` cada 15 minutos en el
Programador de tareas de Windows.

**Correo** (opcional): define en `.env` `MES_EMAIL_HOST`, `MES_EMAIL_PORT` (587), `MES_EMAIL_USER`,
`MES_EMAIL_PASSWORD`, `MES_EMAIL_TLS` (1), `MES_EMAIL_FROM` y `MES_BASE_URL` (dirección de la app, para los enlaces).
Cada usuario recibe los avisos en el correo de su ficha de usuario. Sin `MES_EMAIL_HOST` los avisos quedan solo en la app.

## Logística: mercar pedidos

Rol **Logística** (grupo *Logística*): «mercar» es alistar la mercancía del pedido y dejarla ubicada para producción.
Al entrar va directo a **Mercar pedidos** (`/logistica/`): tarjetas con la información del pedido (cliente, producto,
cantidad, servicio, inicio, compromiso, despacho, línea, supervisor, OP/OC/referencia/embarque y observaciones del
pedido). No modifica nada del pedido; solo **«Marcar como mercado»** con tres datos: **quién mercó** (viene con su
nombre), **ubicación de la mercancía** (sugiere las usadas hace poco) y **observaciones** (opcional).

- Aparecen los pedidos ya planeados y no terminados (planificada, en insumos, liberada, en proceso). Pestañas
  *Por mercar* / *Mercados*; el menú muestra cuántos faltan.
- Al marcarlo, la mercancía del pedido queda al 100 % en el tablero de capacidad. «Corregir datos» cambia
  quién/ubicación/observaciones; «Volver a por mercar» exige el motivo (queda en las observaciones). Todo va a la bitácora.
- El supervisor ve en **Mi turno** si la mercancía de cada operación está *Por mercar* o *Mercancía en <ubicación>*; el
  detalle del pedido tiene la tarjeta «Mercancía (logística)».
- El pedido no pasa a planta hasta que esté mercado (ver «Liberación automática a planta» en Insumos). Si insumos ya
  liberó, marcarlo como mercado lo pasa a planta en ese momento.

## Validación EAN: referencias y cantidades de cada pedido (`plugins/eanes`, `/p/eanes/`)

Un pedido puede traer varias referencias (EAN) y cada una con su cantidad. La idea viene de la app vieja (hoja
«Validacion eanes»): planta solo puede trabajar las referencias del pedido y, de cada una, solo las unidades que trae.
Esta primera parte es la **carga**: la hace una persona aparte, no planeación ni quien monta el pedido.

- **Rol «Validación EAN»** (grupo que se crea solo con `migrate`): al entrar va directo a su cola. Solo ve los pedidos;
  no los planea ni los modifica. Planeación y el jefe de planta pueden consultar los EAN pero no cargarlos.
- **Cola** (`/p/eanes/`): cada pedido abierto llega como *Pendiente* apenas se crea (y le llega un aviso a la
  bandeja, salvo en la carga masiva desde Excel). Pestañas *Pendientes* (el más antiguo primero, con cuánto lleva
  esperando) / *Con EAN* (dice si cuadra con la cantidad del pedido) / *No aplica*. Busca por pedido, cliente, OP, OC
  o un EAN. Abre en la planta con más pedidos esperando.
- **Cuatro formas de cargar**; nada se guarda sin pasar por **Revisión**:
  1. *Pegar desde Excel*: copiar las columnas EAN y cantidad (y descripción) y pegar. El orden de las columnas da igual;
     el encabezado se ignora. También vale escribir `7705675995066 122` por línea; `sin límite` = referencia sin tope.
  2. *Archivo del cliente* (.xlsx o .csv): busca solas las columnas del EAN y de la cantidad (por nombre: EAN, código de
     barras, PLU, UPC, cantidad, unidades… o por contenido), salta títulos y filas de TOTAL. Si no acierta, se eligen
     a mano y se **recuerdan para ese cliente**.
  3. *Escanear* con la pistola: cada lectura suma las «unidades por lectura» (1 = contar unidad por unidad); avisa con
     sonido si el código está mal leído; lo escaneado queda en el equipo hasta confirmarlo.
  4. *Copiar de otro pedido* (primero los del mismo cliente y producto) y ajustar.
- **Revisión**: valida el dígito de control de cada EAN (detecta códigos mal digitados), guarda los UPC de 12 dígitos
  con el 0 adelante (el escáner Zebra lee de las dos formas y siempre coinciden), **suma los EAN repetidos**, avisa
  si los EAN no suman la cantidad del pedido y lista las líneas con problema (se corrigen, o se guardan solo las
  válidas marcándolo). Con lista ya cargada se elige *Reemplazar* o *Sumar*.
- **No lleva EAN**: apoyos, procesos internos… se marca *No aplica* (planta no tendrá que escanear) y se puede reabrir.
- **Control**: si el pedido ya está en producción, cambiar sus EAN exige el motivo. El historial guarda quién cargó o
  cambió qué (agregados, quitados, cantidades antes → después). «Descargar (CSV)» exporta la lista; «Guardar y seguir
  con el siguiente pendiente» encadena la cola. El detalle del pedido tiene el botón «EAN del pedido / EAN: pendiente».
- Las reglas viven en `plugins/eanes/eans.py`, sin depender de las pantallas, para llevarlas a la app nueva.

### Reportar por referencia (como la app anterior)

- **Al reportar** (tablet del operario o de la línea, tablet del supernumerario y formulario del pedido) se elige o
  escanea la referencia. Si el pedido tiene EAN: es obligatoria (salvo un reporte solo de merma), tiene que ser del
  pedido y no puede pasar de lo que le falta **a esa referencia en esa etapa** (sumando sus lotes). El escáner Zebra
  funciona con o sin el 0 adelante.
- **Contador**: arriba se ve la referencia que se está marcando y cuánto le falta; «Cambiar» la escanea o la elige de
  la lista (se recuerda por operación en ese equipo). Si solo queda una referencia abierta, se elige sola.
- **El supernumerario decide** (tablet, botón «Referencias (EAN)» de cada operación de sus líneas): el **orden de
  trabajo** (↑ ↓) y qué referencias están **abiertas**. *Cerrar* corta el envío de datos a esa referencia (tablets y
  formulario) hasta que la vuelvan a abrir; *Solo esta* abre una y cierra las demás; *Abrir todas*. Vale por etapa
  (todos sus lotes). Al llegar al 100 % la referencia no recibe más datos aunque esté abierta. Las correcciones de
  reportes viejos en el MES no se bloquean. Cada cambio queda en el historial de EAN del pedido; sin cambios, todas
  abiertas y en el orden del pedido (`EanControl`).
- **Códigos sobrantes**: en los reportes por tramo y en el pedido. Completan la referencia y lo programado de la etapa,
  pero no son producción de nadie (no entran en eficiencia ni puntos).
- Pedido «No aplica EAN»: se reporta sin referencia. Pedido aún sin EAN cargados: se deja reportar sin referencia (con
  aviso); con `MES_EAN_EXIGIR=1` en el entorno no se puede reportar hasta que los carguen.
- **Estado por referencia** (Pendiente / En proceso / Completado / Sin límite) en cada etapa: «Avance por referencia»
  en la página de EAN del pedido y en su seguimiento.

## Registros, seguimiento y exportación (`plugins/registros`, `/p/registros/`)

Lo que se hacía con la hoja de cálculo de la app anterior. Lo ven planeación, jefe de planta y supervisores.

- **Registros de producción** (`/p/registros/`): una fila por persona y por reporte, como la hoja «Registros». Filtros:
  fechas, pedido/OP/OC, operario (nombre o documento), EAN, proceso, cliente, coordinador (supervisor),
  supernumerario, línea y estado (activos / anulados / todos). Horas encadenadas como en la app anterior: cada
  marcación cuenta desde la anterior de la misma persona en el día (o desde el inicio del tramo), sin paros; una pausa
  de más de 90 min no cuenta. Los reportes hechos a mano en el MES no tienen horas.
- **Corregir con motivo** (obligatorio, queda quién y cuándo): unidades, merma, sobrantes, referencia, operario, pedido
  /etapa (pasar el reporte a otra operación) y fecha-hora. Revalida que quepa en la operación y en la referencia; las
  marcaciones de piso se ajustan solas (en proporción si el reporte se repartió entre varias personas).
- **Anular** (con motivo): el reporte sale de todas las sumas (operación, pedido, referencia, eficiencia, puntos) y
  queda en «Anulados» completo. **Reactivar** (con motivo) lo devuelve tal cual si todavía cabe. **Eliminar** solo se
  puede con lo anulado (planeación / jefe de planta). En el pedido, «Eliminar» pasó a ser «Anular»; anular desde la
  tablet (30 min) también deja el reporte en «Anulados».
- **Seguimiento de pedidos** (`/p/registros/seguimiento/`): como «Seguimiento de paquetes». KPI (pedidos, pendientes,
  en proceso, Cumple, No cumple, eficiencia promedio), gráficas de estado y eficiencia, y por pedido: procesadas
  (última etapa), pendientes, horas, eficiencia, supervisores y supernumerarios. El detalle muestra etapas, avance por
  referencia, registros y anulados.
- **Cerrar con faltante justificado** (en el detalle del seguimiento): lo que falta de cada etapa queda justificado
  por referencia, con motivo y quién justifica; el pedido pasa a Terminado · **No cumple**. Si se termina completo es
  **Cumple**. «Deshacer el cierre» reabre las etapas (p. ej. llegó la mercancía).
- **«Mis registros»** en la tablet del operario (pestaña Historial): lo suyo de una fecha o sus últimos 100, con
  unidades, horas y eficiencia (como «Control tiempos»).
- **Exportar a Excel** (`/p/registros/exportar/`): de una fecha a otra (atajos: hoy, ayer, 7 días, este mes, mes
  anterior, este año), con las mismas hojas y columnas del libro «Control de la produccion»: *Registros*,
  *Seguimiento paquetes*, *Validacion eanes* y *Control_Horas* (horas registradas frente a las del turno). Al final de
  cada hoja van unas columnas que el MES agrega (sobrantes, merma, línea, etapa…). Opcional: incluir los anulados.
  Sublínea y evento quedan vacíos: el MES no los maneja.

## Planeación de etapas: supervisor, rendimiento y horas extra

En cada etapa de `/planeacion/<pedido>/`, sin salir de la pantalla:

| Campo | Qué hace |
|---|---|
| **Supervisor responsable** | «Automático» = lo pone la regla de supervisor por línea y turno (debajo se ve a quién: «Por regla: Ana Ruiz (ETQ · Mañana)» o «sin regla»). Elegir uno a mano lo deja fijo en todas las operaciones de la etapa (la regla ya no lo cambia). Solo quien puede asignar supervisores lo cambia. |
| **Rendimiento de la gente (%)** | Tiempo real por unidad = estándar ÷ rendimiento (0,5 min/u al 85 % = 0,59 min/u). Vacío = el medido de la línea (Administración → Centros de trabajo) o el de la planta (Reglas de planeación, por defecto 100 %). El botón «Usar medido» toma el rendimiento real de la línea en los últimos 30 días (producción reportada a tiempo estándar ÷ horas-hombre trabajadas). |
| **Horas extra de esta etapa** | «Alargar turno»: elige el final de jornada de la línea (p. ej. «Tarde del jue 19/11, después de las 22:00») y cuántas horas. Se ven como fichas y se pueden quitar. |

Cada etapa muestra **cuántos turnos necesita y cuántos caben antes del compromiso** (p. ej. «Necesita 2,1 turnos y caben
2,0: faltan 0,1 turno ≈ 0,8 h de horas extra»).

**Sugerir horas extra para cumplir** (en el resumen) busca, recalculando el plan completo, la menor cantidad de horas
extra con la que el pedido cumple:

- **Alargar el último turno** antes del compromiso de cada línea que lo necesita (2,1 turnos → uno de 1,1 y uno normal).
- **Repartir** la misma cantidad al final de varios turnos (menos cansancio por persona).
- **Turno adicional completo** en un hueco sin trabajo (noche, fin de semana, festivo): 2 turnos normales + 1 extra.

Las horas extra van a las líneas de las etapas que las usan (las etapas van en cadena: alargar una sola no adelanta a
las siguientes), se recortan a lo que realmente se usa y se les suma el margen del **rendimiento en horas extra**
(Reglas de planeación, por defecto 90 %: si faltan 0,9 h se proponen 1 h). Cada opción dice si cumple, la holgura,
las horas y las horas-hombre extra. «Usar esta opción» la pone en las etapas; al **Guardar y programar** se crean en el
Calendario laboral, solo para esas líneas y ligadas al pedido (re-planear las reemplaza). Si nada alcanza, lo dice y
avisa si un mantenimiento lo impide (una operación no atraviesa un mantenimiento).

### Takt time frente al tiempo de ciclo

En el resumen de la planeación por etapas (se recalcula en vivo):

| Indicador | Cálculo |
|---|---|
| **Takt time** | Minutos hábiles desde el inicio hasta el compromiso (turnos sin descansos, calendario laboral) ÷ unidades del pedido. Es el ritmo al que debe salir 1 pieza para cumplir. |
| **Tiempo estándar de 1 pieza** | Σ minutos por unidad del estándar de todas las etapas (contenido de trabajo del proceso), y cuántas veces el takt es. |
| **Tiempo real de 1 pieza** | Σ minutos por unidad reales (estándar ÷ rendimiento). |
| **Ciclo de cada etapa** | Minutos reales por unidad ÷ operarios de la etapa: cada cuánto sale una pieza. Barra por etapa con la línea del takt; en rojo si lo supera. |
| **Ciclo del proceso** | El de la etapa más lenta (cuello de botella). Si supera el takt, el pedido no sigue el ritmo. |
| **Piezas por hora** | Necesarias (60 ÷ takt) frente a las que hace el cuello de botella. |
| **Operarios mínimos** | ⌈tiempo real de 1 pieza ÷ takt⌉ con el trabajo balanceado, frente a lo que asigna el plan. |

El alistamiento no entra en el ciclo por pieza; se informa aparte con el takt descontándolo. Cada etapa muestra además
«Ciclo / takt» en sus indicadores. Sin fecha compromiso no hay takt.

### Etapas en paralelo: en cola, a la par o al mismo tiempo

Entre una etapa y la siguiente, la planeación pregunta la **relación con la etapa anterior**:

| Relación | Qué significa | Ejemplo |
|---|---|---|
| **En cola** (por defecto) | Empieza cuando termina la anterior. Con lotes, el lote *i* espera al lote *i* de la anterior si ambas tienen los mismos lotes. | Primero presinto en toda la camisa, luego etiquetas. |
| **A la par** | Trabaja al mismo tiempo que la anterior: le va llegando producto. Arranca cuando la anterior lleva *N* unidades (campo «Arranca cuando la anterior lleve», 1 si está vacío) y no termina antes que ella: si es más rápida, arranca más tarde para no quedarse esperando. | Un módulo pone presintos y el de al lado etiqueta las camisas que ya lo tienen. |
| **Al mismo tiempo** | Arranca junto con la etapa que abre el grupo, sin esperar producto de la anterior. | Dos módulos con material propio. |

Una etapa «en cola» abre un grupo; «a la par» y «al mismo tiempo» se suman al grupo de la anterior, y la siguiente
etapa «en cola» espera a **todo** el grupo. El modo «Terminar antes del compromiso» reparte el tiempo por grupos (las
etapas de un mismo grupo comparten la ventana). «Máximo de operarios a la vez» suma las cuadrillas que coinciden.
Si dos etapas del mismo grupo usan la misma línea, avisa para verificar que la línea tenga módulos para las dos.

En el **programador** y el **Gantt** los bloques muestran ⤳ (a la par) o ∥ (al mismo tiempo), el detalle dice la
relación y el botón **«Relación con la etapa anterior»** la cambia (con la opción de reprogramar la orden en el
momento). El plan automático y la detección de conflictos de secuencia respetan la relación.

### Editar todo y corregir lo reportado (sin pasar por Administración)

- **Planeación › «Editar todo»**: en un pedido que ya empezó o está terminado se habilita toda la planeación (agregar,
  quitar y cambiar etapas, unidades, líneas…). Antes de guardar avisa qué se borra: las operaciones actuales con sus
  reportes de producción, cuadrillas y marcaciones de piso. El pedido vuelve a «Liberada» con 0 terminado.
- **Pedido › Editar**: todos los datos del pedido (cliente, producto, cantidad, fechas, prioridad, referencias,
  estado…) en una pantalla del MES. La planta solo cambia si el pedido aún no tiene operaciones.
- **Pedido › Historial de reportes**: cada reporte se puede **editar** (buenas, merma, si cierra la operación) o
  **eliminar**, y hay «Eliminar todo lo reportado». La operación y el pedido se recalculan solos (cantidades y estado)
  y, si el reporte venía de la tablet de piso, se borran sus marcaciones.
- Lo hacen planeación, el jefe de planta y los administradores; cada cambio queda en la bitácora del programador.

### Re-planear lo pendiente de una orden que ya empezó

Si la orden ya tiene operaciones terminadas o en proceso, la planeación por etapas muestra **«Re-planear lo
pendiente»**. Lo terminado y lo que está en proceso **no se toca**; se vuelve a programar solo lo pendiente de cada etapa
(las unidades son las que faltan y no se pueden agregar, quitar ni reordenar etapas). Se puede cambiar línea, operarios,
rendimiento, horas extra, supervisor y relación entre etapas. Lo pendiente no arranca antes de que termine lo que la
etapa tiene en curso ni antes de lo ya hecho en las etapas de las que depende; los lotes nuevos siguen la numeración
(lote 3, 4…). Una etapa sin pendientes se muestra como terminada. El takt se calcula sobre las unidades pendientes.

## Indicadores (`/indicadores/`)

Menú Operación → **Indicadores** (planeación y jefe de planta), para todas las bodegas o una. El tablero de la
empresa muestra los mismos indicadores de los últimos 30 días.

**Periodo.** Atajos (hoy, ayer, esta semana, semana pasada, este mes, mes pasado, últimos 30 y 90 días) o un rango
a mano con el calendario (*Desde / Hasta*, hasta 400 días); ‹ › pasan al periodo anterior o siguiente del mismo
largo. Todo se compara con el periodo anterior. La tendencia se adapta al periodo: un día o un rango corto → los
últimos 8 días; una semana → las últimas 8 semanas; hasta 31 días → día a día; hasta 26 semanas → semana a semana;
más → mes a mes. Los periodos cerrados se guardan 10 min en caché. **En vivo:** si el periodo incluye hoy, la
pantalla se actualiza sola cada minuto (se puede pausar; no recarga con la pestaña oculta ni mientras se escriben
fechas). La dirección acepta `?periodo=hoy|semana|…`, `?desde=AAAA-MM-DD&hasta=AAAA-MM-DD` y el antiguo `?semana=`.

| Indicador | Cálculo | Bien / a vigilar / crítico |
|---|---|---|
| **OEE** (línea, bodega, empresa) | Disponibilidad × Rendimiento × Calidad. Disponibilidad = tiempo produciendo (inicio → fin real, menos paros de línea) ÷ tiempo planeado (horas hábiles con operación programada o trabajando, sin descansos ni mantenimientos). Rendimiento = minutos estándar producidos ÷ minutos-persona produciendo. Calidad = buenas ÷ (buenas + rechazos + reprocesos). | ≥ 85 % / ≥ 60 % / menos |
| **OLE** (bodega, empresa) | La misma fórmula sobre la gente: planeado = personas del reparto × horas hábiles de cada turno; productivo = minutos-persona produciendo − paros individuales − pausas excedidas. Ahí caen ausencias, llegadas tarde y gente sin tarea. | ≥ 85 % / ≥ 60 % |
| **Pareto de paros** | Minutos de paro de piso por motivo, con % acumulado; se marcan los motivos que suman el 80 %. | — |
| **Pausas excedidas** | Desayunos y almuerzos que pasaron de su fin previsto: cuántas y minutos de más, por línea y por supervisor. | — |
| **Alistamiento y cambios** | Paros de toda la línea con motivo de alistamiento / cambio / esperando patinador (en la tablet: «Alistamiento (esperando patinador)») ÷ tiempo planeado; promedio real por cambio frente al alistamiento planeado de las operaciones (base para SMED). | — |
| **MTBF / MTTR** (línea) | Los demás paros de toda la línea, sin alistamientos ni pausas. MTBF = tiempo produciendo ÷ nº de paros; MTTR = minutos de paro ÷ nº de paros. | — |
| **TEEP** (línea, total) | OEE × tiempo planeado ÷ calendario (24 h todos los días) = OEE × carga del turno (programado ÷ horas de turno) × utilización (horas de turno ÷ calendario). El total solo suma las líneas con trabajo. Lectura por línea: turno lleno (≥ 90 %) con buen OEE → otro turno si faltan horas varios días, horas extra si es puntual; turno lleno con OEE < 60 % → primero recuperar pérdidas; < 60 % de carga → no hacen falta horas extra. | sin color: un TEEP bajo con un turno es normal |
| **OTIF** | Pedidos terminados antes del compromiso y sin cierre con faltante ÷ pedidos terminados. | ≥ 95 % / ≥ 85 % |
| **Adherencia al plan** | Operaciones que terminaron hasta 30 min después de su fin planeado ÷ las que debían terminar; por bodega y supervisor. | ≥ 90 % / ≥ 75 % |
| **Puntualidad** | Llegadas confirmadas en la tablet hasta 5 min después de la entrada del turno ÷ llegadas. | ≥ 95 % / ≥ 85 % |
| **Respuesta a horas extra** | De las convocatorias enviadas: % que respondió, % que aceptó, % de las personas pedidas que se cubrió y motivos del «no». | ≥ 70 % / ≥ 40 % aceptan |

Paros, pausas, reprocesos, llegadas y convocatorias vienen del control de piso; sin ese módulo esos datos quedan en
cero y el resto se calcula igual. Un rendimiento por encima del 100 % avisa que el tiempo estándar está desactualizado.

## Bolsa global de personal: capacidad global, reparto y quién está dónde

Todos los operarios activos forman **una sola bolsa para toda la empresa** (p. ej. 255). Nadie es fijo de una bodega.

**Tablero de la empresa** (Capacidad de planta → «Todas las bodegas», la vista de inicio): personal total, libres para
repartir, ausencias y ausentismo, **capacidad disponible** = presentes × jornada (horas hábiles del turno más común, sin
descansos), lo programado hoy y en 7 días frente a esa capacidad, trabajo sin programar (en jornadas de toda la bolsa),
horas extra de los próximos 7 días, y el desempeño de 30 días con lo reportado en piso: rendimiento real (minutos
estándar ÷ minutos-hombre), unidades por hora-hombre, calidad, pedidos a tiempo, abiertos, atrasados y en riesgo.

| Pantalla | Qué hace |
|---|---|
| **Capacidad global** (`/capacidad-global/`) | Personal total, repartidos, sin repartir y ausentes; por día y bodega: personas y horas-hombre programadas / disponibles con su % de ocupación, y la fila «Toda la empresa». |
| **Reparto del personal** (`/personal/reparto/`) | De la bolsa, cuántas personas trabaja cada bodega y turno. **Reparto base** (todos los días) y **cambios por fecha** («el lunes 40 a la 224»), que ganan sobre el base. Muestra el reparto de un día con lo que queda sin repartir y avisa si el reparto supera la bolsa. |
| **¿Quién está dónde?** (`/personal/ubicacion/`) | Cada persona en un momento, por bodega, frente al reparto («faltan 28», «sobran 86»). Se seleccionan personas y se **mueven** a otra bodega o línea por unas horas o hasta nuevo aviso; **Devolver** termina el traslado vigente. |

Reglas:

- **Operarios disponibles** de un turno (planeación por etapas, programador, plan automático, capacidad): el reparto de
  esa bodega, turno y fecha. Un cambio por fecha gana sobre el base; un reparto con turno gana sobre el de toda la
  bodega. Donde no hay reparto se usa lo de antes: los operarios del turno o, si es 0, el total de la planta.
- **Dónde está una persona** en un momento: 1) la cuadrilla en la que está asignada (la operación da bodega y línea),
  2) el traslado más reciente que cubre ese momento, 3) su bodega habitual. Un traslado temporal vence y la persona
  vuelve a donde estaba; uno «hasta nuevo aviso» cierra los anteriores.
- Una persona trabaja un turno al día: el reparto de un día suma todos los turnos de todas las bodegas.
- Roles: *Planeación* y *Jefe de planta* configuran el reparto y mueven personal; *Supervisor* ve el reparto y mueve
  personal; nadie más lo ve.

## Personal: supervisores, asistencia y cuadrillas

Menú **Personal** (roles: *Planeación* asigna supervisores y gestiona a todo el personal; *Supervisor* ve a **su
equipo**, mantiene su polivalencia, confirma asistencia y arma cuadrillas; *Jefe de planta* ve y hace todo lo de ambos).

**El personal se carga solo desde el panel de administración** (Administración → Operarios), no desde la aplicación.
El supervisor no crea ni edita operarios.

**El personal es general**: no pertenece a una planta (la «planta habitual» quedó como dato opcional e informativo).
Operarios, matriz de polivalencia, cuadrillas y cargas desde Excel trabajan con todo el personal; el turno habitual puede
ser de cualquier planta (si dos plantas tienen un turno con el mismo nombre, en el Excel se escribe «PLANTA · turno»).

**Asistencia en lista general** (Mi turno): cada supervisor ve a todo el personal y marca presentes a los que tiene
con él; quedan a su nombre («Contigo» / «Con Marta»). Filtros: Todos, Conmigo, Sin confirmar y Mi equipo (el equipo
habitual asignado en Administración). Guardar solo cambia las filas que el supervisor tocó, así que no le quita a
otro supervisor los que ya marcó. Los indicadores (presentes, ausentismo, sin confirmar) son generales. Al vencer el
plazo de un turno se avisa al jefe de planta cuántos siguen sin confirmar y qué supervisores con operaciones no han
marcado a nadie (y a esos supervisores se les recuerda).

| Pantalla | Quién | Qué hace |
|---|---|---|
| **Administración → Operarios** (`/admin/mes_core/employee/`) | Administrador (usuario con acceso al panel) | Alta manual y **«Cargar desde Excel»** (con plantilla). En la lista, **turno, supervisor y activo son desplegables**: se cambian ahí mismo y «Guardar», sin entrar a cada operario. Acciones masivas sobre los marcados: **Asignar supervisor**, **Asignar turno** (solo a los de la planta del turno), **Asignar competencia y nivel** (o quitarla), Activar / Inactivar. Filtros por planta, turno, supervisor y competencia. |
| **Operarios** (`/personal/`) | Planeación, Supervisor (su equipo) | Base de personal por planta: documento, nombre, cargo, turno habitual, supervisor y competencias. Planeación edita datos y tiene acciones en bloque (supervisor, turno, estado); el supervisor solo consulta. |
| **Polivalencia** (`/personal/polivalencia/`) | Planeación, Supervisor | Matriz operario × competencia con nivel **novato / intermedio / experto**. Se llena a mano celda por celda (se guarda sola). Bajo cada competencia: cuántos expertos, intermedios y novatos hay. |
| **Asignar supervisores** (`/supervision/supervisores/`) | Planeación | Operaciones programadas en un rango de fechas, agrupadas por pedido, con los operarios que calculó planeación. Se elige el supervisor por operación o «Asignar todas a…» por pedido. Panel de carga por supervisor. |
| **Mi turno** (`/supervision/`) | Supervisor | 1) Asistencia del día de su equipo (presente, tarde, ausente, permiso, incapacidad, vacaciones; «Marcar presentes a los sin confirmar»). 2) Sus operaciones del día con cuadrilla `asignados / requeridos`. |
| **Armar cuadrilla** (`/supervision/operacion/<id>/`) | Supervisor responsable | Lista de operarios ordenada por: disponibles → confirmados presentes → mejor nivel en la competencia de la operación → de su equipo. «Sugerir N» escoge los mejores. |

Reglas:

- **Normalización automática** al guardar (formulario, matriz y Excel): minúsculas, sin tildes ni diéresis (la ñ se
  conserva), espacios repetidos fuera; documento sin puntos ni espacios (`1.020.345.678` → `1020345678`). Las
  competencias son texto libre: «Control_de CALIDAD.» y «control de calidad» son la misma. Los niveles aceptan sinónimos
  (`EXPERTO`, avanzado, 3, básico, medio…).
- **Excel** (`.xlsx` o CSV): Administración → Operarios → «Cargar desde Excel» → «Descarga la plantilla» (trae listas de turnos, supervisores y
  niveles). Obligatorio: documento y nombre; opcional: cargo, turno, supervisor (usuario o nombre), teléfono,
  conocimientos, activo, y **una columna por competencia** con el nivel en la celda. El documento identifica al operario:
  si existe, se actualiza. Acepta títulos encima de los encabezados y encabezados con sinónimos (cédula, nombres +
  apellidos, celular…). Todo o nada: si una fila falla no se guarda ninguna y se lista qué corregir.
- Una persona **no se puede asignar** si está marcada ausente/permiso/incapacidad/vacaciones ese día o si ya está en otra
  operación que se cruza en horario. Asignar más de lo planeado se permite y se avisa.
- **La cuadrilla asigna el supervisor solo**: al elegir a alguien para una cuadrilla queda con el supervisor
  responsable de esa operación (o con quien armó la cuadrilla, si la operación no tiene). Si era libre aparece «con
  ese supervisor · cuadrilla OP-…»; si tenía supervisor habitual, se le guarda. Cuando la operación termina, o lo sacan
  de la cuadrilla, vuelve a quedar **libre** (o con su supervisor de siempre); si está en otra cuadrilla abierta pasa
  a esa. Si planeación cambia el supervisor de la operación, la cuadrilla pasa con el nuevo. Si alguien cambia el
  supervisor a mano, queda fijo y ya no se devuelve solo. La revisión automática (cada 5 min) libera también lo que se
  cierre por otros caminos (pedido cancelado, cierre masivo).
- La asistencia confirmada **actualiza las ausencias** del tablero de capacidad (por turno y día; el turno de noche cuenta
  para el día en que termina).
- Al **re-planear** una orden se conserva el supervisor de cada etapa; las cuadrillas se rearman (la bitácora lo indica).
  Al dividir una operación, las partes heredan el supervisor.
- `seed_demo` crea operarios de ejemplo con su matriz si la planta no tiene (no crea usuarios: crea los supervisores
  en Administración y agrégalos al grupo *Supervisor*).

## Control de piso: tablets de los operarios (`plugins/piso`, `/p/piso/`)

La antigua app de control de piso (carpeta `app/`, Node) quedó migrada a Python como un módulo del MES: arranca con el
mismo servidor y usa los mismos datos. Los supervisores deciden desde el MES; la tablet solo ejecuta.

**Estándar por hora (ritmo).** Cada pantalla muestra cuántas unidades deberían salir por hora según el tiempo
estándar de la operación (`60 ÷ minutos por unidad`, por persona): el operario ve su estándar, cada 15 min, «1 unidad
cada» y cómo va en la última hora; la tablet de línea, el de la cuadrilla (× personas); el supernumerario, el de la
línea y cuántas deberían ir en el tramo que está por reportar (también en el formulario del reporte).

| App original (Node) | En el MES |
|---|---|
| Operario (`ops`) | Operario del personal general (`Employee`) |
| Módulo (`mods`) | Línea / centro de trabajo (`WorkCenter`) |
| Referencia con SAM (`refs`) | Operación del pedido (`minutes_per_unit` = minutos estándar por unidad) |
| Lote | Pedido liberado a planta |
| Tarea actual y cola (`cola`) | Operaciones a las que el supervisor asignó al operario en «Mi turno», en el orden del programa |
| Horario por operario (ingreso, desayuno, almuerzo) | Turno del operario (el de su asistencia del día o su turno habitual) |
| Usuarios con rol | PIN de tablet por operario (Administración › Operarios) y código por línea |

- **Entrada**: el operario usa su documento + PIN. El administrador da el acceso en *Administración › Operarios* con la
  acción «Tablet de piso: dar acceso y generar PIN» (descarga un Excel con los PIN; solo se ven en ese momento). Si
  cambia el PIN, la sesión de la tablet se cierra. El operario puede cambiar su PIN en Ajustes.
- **Tarea**: la tablet muestra la operación en curso del operario (pedidos liberados a planta), la meta, lo que falta y
  su cola. Cada **marcación** (unidad buena o rechazo) entra al MES al instante como **reporte de producción** del
  operario (`source = "piso"`); las de 15 minutos se acumulan en un solo reporte. Los reprocesos solo quedan en piso.
  Al completar lo programado la operación se cierra sola y pasa a la siguiente de la cola.
- **Tablet de línea** (el antiguo «módulo»): se vincula con el código de la línea (o escaneando su QR) y marca por toda
  la línea; registra paros de cada persona de la cuadrilla o de toda la línea y pide material a los patinadores.
- **Paros**: con motivo; mientras hay un paro abierto no se marcan unidades. **Pausas**: desayuno y almuerzo salen del
  turno; con un solo horario arrancan solas, con grupos escalonados el operario la inicia. Para volver se escanea el
  **QR de pausas** pegado en la línea (o lo cierra el supervisor desde *Piso en vivo*).
- **Piso en vivo** (supervisor, jefe de planta): tablero por operario (eficiencia, OEE, unidades, sin marcar > 10 min,
  paros con cronómetro), líneas, paros abiertos y **mensajes a la planta** (a una persona, una línea, varias, baja o alta
  eficiencia). El supervisor ve solo sus operaciones; quien asigna supervisores ve todo.
  En el tablero se ve **quién tiene la app abierta** (punto verde, filtro «Conectados» y el indicador «Conectados a la
  app»), aunque no esté en ninguna operación. Al **tocar la tarjeta** de una persona queda elegida para el mensaje (en
  «Varias personas» cada toque agrega o quita).
- **Alarmas que suenan solas (configurar cada terminal UNA vez)**: un navegador común no deja sonar una página hasta que
  alguien la toca (protección de Chrome y Safari; no se puede saltar desde el código). Para que la alarma suene sin que
  el operario haga nada, el supervisor deja cada equipo así, una sola vez (vale para todos los que lo usen):
  1. Abrir la app por la dirección **https** del MES (p. ej. la de serveo). En la red local por `http://IP:8508` no se
     puede instalar; en Android se puede habilitar en `chrome://flags` › «Insecure origins treated as secure» con esa
     dirección.
  2. En la app: *Ajustes › Este equipo* › **📲 Instalar** (o menú ⋮ › «Instalar app» / «Agregar a la pantalla
     principal») y abrirla siempre desde ese ícono. Instalada, Chrome deja sonar la alarma sin toques.
  3. *Ajustes › Este equipo* › **🔔 Activar** avisos del sistema: el mensaje suena también con el tono de notificaciones
     del teléfono, aunque la app esté en segundo plano.
  4. Volumen al máximo y «Mantener pantalla encendida». Con la pantalla apagada ningún navegador puede sonar.
  - Alternativas para tablets fijas: un navegador de quiosco con reproducción automática (p. ej. Fully Kiosk Browser,
    opción «Autoplay audio/video»); en PC, Chrome con `--autoplay-policy=no-user-gesture-required` en el acceso directo.
  - Además, cualquier toque que el operario haga de todos modos (entrar con su PIN, marcar, aceptar) deja el sonido
    activo en esa pestaña. Nunca se deja un sonido en cola: si el equipo no deja sonar, no suena tarde al dar «Aceptar».
  - Cada equipo le informa al MES si puede sonar solo: en *Piso en vivo* las tarjetas muestran «🔇 alarma sin sonido» (y
    la tablet de cada línea), para configurar ese equipo.
- **Rendimiento con muchas tablets**: SQLite trabaja en modo WAL (se lee mientras otro escribe) con transacciones
  `IMMEDIATE` y espera de 30 s, y las escrituras frecuentes de la planta toman turno en orden de llegada
  (`mes_core/dbqueue.py`): sin errores «database is locked» ni esperas largas. Las sesiones se leen de memoria
  (`cached_db`). Las tablets no encadenan cargas (una a la vez), no vuelven a pedir lo que ya tienen y no redibujan la
  pantalla mientras el dedo está tocando un botón. Con WAL, la base son tres archivos (`db.sqlite3`, `db.sqlite3-wal`,
  `db.sqlite3-shm`): para respaldar en caliente usa `python manage.py dbshell` › `.backup respaldo.sqlite3`, o detén el
  servidor y copia los tres. Arranca con `iniciar.bat` (sin modo depuración: más rápido).
- **Cierre diario de sesiones**: todos los días a las 22:00 (`PISO_CIERRE_SESIONES`, vacío = no cerrar) se cierran solas
  las sesiones de todas las personas en las tablets (operarios, supernumerarios y patinadores): las terminales son
  compartidas y al otro día cada quien entra en la que le toque. Quien entra después de las 22:00 sigue hasta el corte
  siguiente. Las tablets fijas de línea no se desvinculan.
- **Histórico de piso**: totales por operario y por día entre dos fechas, con paros y puntos.
- **Traslados**: patinadores (acción «dar acceso como PATINADOR»), ubicaciones, traslados pendiente → aceptado →
  cargado → entregado, pedidos de material desde la línea y tiempo por ruta aprendido (mediana de mínimo 3 viajes).
- **Tablets y QR**: dirección para las tablets, código de cada línea y QR de pausas imprimibles (requiere `segno`).
- **Forma de reporte** (*Control de piso › Forma de reporte*): cómo llega la producción de cada operación.
  - *Contador*: módulos y mesas; un toque por unidad (lo de arriba).
  - *Cada operario reporta cada cierto tiempo*: líneas continuas (banda, Vélez…). La pantalla del operario muestra una
    cuenta regresiva hasta el próximo corte (inicio del turno + N × el intervalo); al llegar a cero vibra y suena, y el
    operario escribe lo que hizo en el tramo (desde su último reporte).
  - *El supernumerario reporta por la línea*: el asistente del supervisor (acción «dar acceso como SUPERNUMERARIO»)
    escribe en su tablet el total de la línea en cada tramo y se reparte entre el personal de la etapa **según el
    tiempo trabajado** (sin paros, pausas ni ausencias) o **en partes iguales**, o lo escribe **por persona**. Entra al
    pedido como un solo reporte de producción; cada persona recibe su parte en su eficiencia y puntos. Puede anular su
    reporte durante 30 minutos; si se borra el reporte en el pedido, se borra también el reparto.
  - Qué aplica: lo que planeación elija en la etapa del pedido (*Reporte en planta* y *Reparto*) → lo configurado para el
    proceso (nombre de la etapa) → lo de la línea → contador. El supernumerario siempre puede escribir cantidades a mano.
  - Líneas del supernumerario: las de su supervisor (el de su ficha), las que le asignen en esta página y las que él
    elija en la tablet (pestaña *Líneas*).
  - Pestaña *Personal* del supernumerario: asistencia confirmada del día (quién la confirmó y cuándo; las sin
    confirmar en rojo), indicadores de su personal (eficiencia del día y de la hora, buenas, rechazos, minutos de paro,
    estado), **paros** por persona o de toda la línea (también puede cerrar pausas) y **mensajes** a una persona, a una
    línea o a todo su personal (con frases rápidas). Su personal: las cuadrillas de sus líneas y el equipo de su
    supervisor.
  - **Alarma (zumbido)**: al enviar un mensaje (en *Piso en vivo* o desde la tablet del supernumerario) se elige
    «🔔 Con alarma (zumbido)» (por defecto) o sin alarma (aparece en silencio); «Solo zumbido» llama la atención sin
    escribir nada («¡Atención! Mira la pantalla»). Con alarma, los mensajes suenan con sirena y zumbido grave, vibran y hacen parpadear la pantalla cada 4 s hasta que se
    toque «Aceptar» (después del primer minuto, cada 20 s); los cortes de reporte suenan tres veces. El navegador solo
    deja sonar después de un primer toque en la pantalla; en *Ajustes* hay «Probar alarma».
  - Carga repartida: cada persona ve su parte de la etapa (p. ej. 2.400 unidades entre 10 de limpieza = 240 cada uno;
    entre 5 de aspersión = 480).
- **Tiempo real**: las pantallas preguntan cada 2,5 s por un número de versión en la caché y solo recargan sus datos
  cuando cambió algo. Con varios procesos de servidor configura una caché compartida (Redis o base de datos).
- **Incentivo (provisional)**: puntos = minutos estándar × `PISO_PUNTOS_POR_MINUTO` (10); valor = puntos ×
  `PISO_VALOR_PUNTO` (1). Ajústalos en `config/settings.py`.

### Llegada (asistencia) y horas extra en la tablet

- **Llegada**: lo primero que ve cada persona al entrar es «Hoy vas a…» (bodega, línea, operación, supervisor,
  supernumerario, turno). Al aceptar queda su asistencia del día en el MES: «Presente», o «Llegó tarde» si confirma
  después de la **Llegada hasta** de su turno (Configuración › Turnos y personal › «Llegada a la terminal»; vacío = sin
  límite). Antes de aceptar el servidor no deja marcar ni reportar nada. Vale 14 h (turnos de noche incluidos).
- **Horas extra** (menú *Control de piso › Horas extra*, `/p/piso/horas-extra/`): planeación arma una convocatoria con
  los días habilitados (entre semana, sábados, domingos; los festivos se reconocen con el calendario laboral), la
  franja de cada día y cuántas personas se necesitan, y la envía: **Todas** las bodegas (envío masivo a todo el
  personal con tablet) o solo a las bodegas y/o turnos que se marquen; el formulario cuenta en vivo a cuántos les
  llega. En la tablet aparece una pantalla que no deja seguir hasta responder: **NO** (con motivo) o **SÍ** (elige
  los días y, con el reloj, hasta qué hora puede). La pestaña **Extras** muestra los días, su horario y quién va.
  En el MES se ve: convocados, respondieron, faltan, SÍ y NO; por día quiénes van, hasta qué hora y las horas
  ofrecidas (y si faltan personas); por tipo (entre semana, sábado, domingo, festivo). *Descargar Excel* trae el
  detalle para planear. Cerrar la convocatoria deja de pedir respuesta.

## Limitaciones conocidas (v0.3)

- Al re-planear lo pendiente, la bolsa de operarios no descuenta las cuadrillas de las operaciones de la misma orden
  que siguen en proceso (solo las de otras órdenes).
- Una etapa «a la par» se programa como un bloque continuo: si es más rápida que la anterior arranca más tarde, en vez
  de trabajar a ratos.
- La bolsa de operarios del planeador sigue siendo un número por planta y turno; las cuadrillas con nombre propio se
  arman después, en «Mi turno».
- Los tiempos estándar de las rutas son de referencia; hay que ajustarlos a la operación real.
- El indicador de disponibilidad del panel usa lo cargado en pantalla; fuera de la ventana visible muestra "Sin datos".
