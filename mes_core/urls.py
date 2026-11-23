from django.urls import path

from . import bom_views as bmv
from . import capacity_views as cv
from . import inbox_views as ibv
from . import order_import_views as oiv
from . import logistics_views as lgv
from . import planner_views as plv
from . import planning_views as pv
from . import risk_views as rkv
from . import scheduler_views as sv
from . import service_type_views as stt
from . import staff_views as stv
from . import supply_views as suv
from . import views
from . import workforce_views as wfv
from . import indicator_views as inv

app_name = "mes_core"

urlpatterns = [
    path("", cv.dashboard, name="dashboard"),
    path("capacidad/ausencias/", cv.save_attendance, name="save_attendance"),
    path("capacidad/recursos/<int:pk>/", cv.save_resources, name="save_resources"),
    path("ordenes/", views.order_list, name="order_list"),
    path("ordenes/nueva/", pv.order_intake, name="order_create"),
    path("ordenes/cargar/", oiv.order_import_page, name="order_import"),
    path("ordenes/cargar/plantilla.xlsx", oiv.order_import_template, name="order_import_template"),
    path("ordenes/<int:pk>/", views.order_detail, name="order_detail"),
    path("ordenes/<int:pk>/editar/", views.order_edit, name="order_edit"),
    path("ordenes/<int:pk>/reportes/borrar-todo/", views.report_delete_all, name="report_delete_all"),
    path("reportes/<int:pk>/editar/", views.report_edit, name="report_edit"),
    path("reportes/<int:pk>/eliminar/", views.report_delete, name="report_delete"),
    path("ordenes/<int:pk>/liberar/", views.order_release, name="order_release"),
    path("ordenes/<int:pk>/cancelar/", views.order_cancel, name="order_cancel"),
    path("ordenes/<int:pk>/enviar-insumos/", suv.order_send_supplies, name="order_send_supplies"),
    # Insumos: inventario, entregas y liberación a planta
    path("insumos/", suv.supplies_queue, name="supplies_queue"),
    path("insumos/pedido/<int:pk>/", suv.supply_order, name="supply_order"),
    path("insumos/inventario/", suv.supply_inventory, name="supply_inventory"),
    path("insumos/inventario/importar/", suv.supply_import, name="supply_import"),
    path("insumos/inventario/plantilla.xlsx", suv.supply_template, name="supply_template"),
    path("insumos/movimientos/", suv.supply_movements, name="supply_movements"),
    path("insumos/listas/", bmv.bom_list, name="bom_list"),
    path("insumos/listas/importar/", bmv.bom_import, name="bom_import"),
    path("insumos/listas/plantilla.xlsx", bmv.bom_template, name="bom_template"),
    path("insumos/listas/<int:pk>/", bmv.bom_edit, name="bom_edit"),
    path("supervision/reglas/", stv.line_supervisors, name="line_supervisors"),
    path("personal/polivalencia/sugerencias/", stv.skill_suggestions, name="skill_suggestions"),
    path("control/riesgo/", rkv.at_risk, name="at_risk"),
    path("avisos/", ibv.inbox, name="inbox"),
    path("avisos/<int:pk>/", ibv.inbox_open, name="inbox_open"),
    path("avisos/leer-todo/", ibv.inbox_read_all, name="inbox_read_all"),
    path("logistica/", lgv.goods_queue, name="goods_queue"),
    path("logistica/pedido/<int:pk>/", lgv.goods_mark, name="goods_mark"),
    path("plan/", views.plan, name="plan"),
    path("plugins/", views.plugin_list, name="plugins"),
    # Configuración de jornada
    path("configuracion/turnos/", pv.shift_settings, name="shift_settings"),
    path("configuracion/tipos-de-servicio/", stt.service_type_list, name="service_type_list"),
    # Planeación por etapas
    path("planeacion/", pv.planning_list, name="planning_list"),
    path("planeacion/calendario/", plv.work_calendar, name="work_calendar"),
    path("planeacion/reglas/", plv.planning_rules, name="planning_rules"),
    path("planeacion/simulacion/", plv.what_if, name="what_if"),
    path("planeacion/horas-extra/", plv.overtime_suggestions, name="overtime_suggestions"),
    path("planeacion/mediano-plazo/", plv.medium_term, name="medium_term"),
    path("planeacion/versiones/", plv.plan_versions, name="plan_versions"),
    path("planeacion/versiones/<int:pk>/", plv.plan_version_detail, name="plan_version_detail"),
    path("planeacion/cumplimiento/", plv.plan_adherence, name="plan_adherence"),
    path("planeacion/api/promesa/", plv.api_promise, name="api_promise"),
    path("planeacion/<int:pk>/", pv.planning_detail, name="planning_detail"),
    path("planeacion/<int:pk>/api/calcular/", pv.api_plan_preview, name="api_plan_preview"),
    path("planeacion/<int:pk>/api/guardar/", pv.api_plan_save, name="api_plan_save"),
    path("planeacion/<int:pk>/api/horas-extra/", pv.api_plan_overtime, name="api_plan_overtime"),
    # Tablero de planta (en vivo)
    path("planta/", sv.plant_board, name="plant_board"),
    path("planta/api/", sv.api_plant_board, name="api_plant_board"),
    # Programador visual
    path("programador/", sv.scheduler, name="scheduler"),
    path("programador/api/tablero/", sv.api_board, name="api_board"),
    path("programador/api/programar/", sv.api_place, name="api_place"),
    path("programador/api/editar/", sv.api_edit, name="api_edit"),
    path("programador/api/redimensionar/", sv.api_resize, name="api_resize"),
    path("programador/api/devolver/", sv.api_unschedule, name="api_unschedule"),
    path("programador/api/auto/", sv.api_auto, name="api_auto"),
    path("programador/api/mantenimiento/", sv.api_downtime_create, name="api_downtime_create"),
    path("programador/api/mantenimiento/<int:pk>/eliminar/", sv.api_downtime_delete, name="api_downtime_delete"),
    path("programador/api/importar/", sv.api_import, name="api_import"),
    path("programador/api/dividir/", sv.api_split, name="api_split"),
    path("programador/api/unir/", sv.api_merge, name="api_merge"),
    path("programador/api/relacion-etapa/", sv.api_stage_link, name="api_stage_link"),
    # Personal: operarios, polivalencia, supervisores, asistencia y cuadrillas
    path("capacidad-global/", wfv.global_capacity, name="global_capacity"),
    path("indicadores/", inv.indicators, name="indicators"),
    path("personal/reparto/", wfv.staff_split, name="staff_split"),
    path("personal/ubicacion/", wfv.staff_location, name="staff_location"),
    path("personal/", stv.employee_list, name="employee_list"),
    path("personal/nuevo/", stv.employee_form, name="employee_create"),
    path("personal/<int:pk>/", stv.employee_form, name="employee_edit"),
    path("personal/importar/", stv.employee_import, name="employee_import"),
    path("personal/plantilla.xlsx", stv.employee_template, name="employee_template"),
    path("personal/polivalencia/", stv.skills_matrix, name="skills_matrix"),
    path("personal/api/nivel/", stv.api_skill_level, name="api_skill_level"),
    path("supervision/", stv.supervision_home, name="supervision_home"),
    path("supervision/supervisores/", stv.supervisor_assign, name="supervisor_assign"),
    path("supervision/operacion/<int:pk>/", stv.crew_assign, name="crew_assign"),
]
