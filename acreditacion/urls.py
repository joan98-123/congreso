from django.urls import path
from . import views

urlpatterns = [
    # Público (Participante / Estudiante)
    path("", views.registro_pasos_view, name="registro_pasos"),
    path("branding/logo-cima.jpg", views.logo_cima_view, name="logo_cima"),
    path("api/publico/consultar-padron/", views.api_consultar_padron, name="api_consultar_padron"),
    path("api/publico/registrar/", views.api_registrar_participante, name="api_registrar_participante"),
    path("api/publico/estado/<str:dni>/", views.api_estado_participante, name="api_estado_participante"),
    path("pase/descargar/<str:token>/", views.descargar_qr_png, name="descargar_qr_png"),

    # Autenticación Staff / Admin
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),

    # Panel Administrativo y Escáner (@login_required)
    path("panel/", views.dashboard_view, name="dashboard"),
    path("panel/exportar-excel/", views.exportar_excel_view, name="exportar_excel"),
    path("escaner/", views.escaner_view, name="escaner"),

    # APIs Administrativas (@login_required)
    path("api/admin/dashboard-data/", views.api_dashboard_data, name="api_dashboard_data"),
    path("api/admin/confirmar-pago/<int:participante_id>/", views.api_confirmar_pago, name="api_confirmar_pago"),
    path("api/admin/anular-registro/<int:participante_id>/", views.api_anular_registro, name="api_anular_registro"),
    path("api/admin/ver-qr/<int:participante_id>/", views.api_admin_ver_qr, name="api_admin_ver_qr"),
    path("api/admin/importar-padron/", views.api_importar_padron, name="api_importar_padron"),
    path("api/admin/sincronizar-sheet/", views.api_sincronizar_sheet, name="api_sincronizar_sheet"),
    path("api/admin/modo-prueba/", views.api_configurar_modo_prueba, name="api_configurar_modo_prueba"),
    path("api/admin/escanear-qr/", views.api_escanear_qr, name="api_escanear_qr"),
]
