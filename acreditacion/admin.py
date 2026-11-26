from django.contrib import admin
from .models import ConfiguracionEvento, LogAsistencia, PadronMatriculado, Participante


@admin.register(PadronMatriculado)
class PadronMatriculadoAdmin(admin.ModelAdmin):
    list_display = ("codigo_alumno", "dni", "apellidos_nombres", "actualizado_en")
    search_fields = ("codigo_alumno", "dni", "apellidos_nombres")
    ordering = ("apellidos_nombres",)


@admin.register(Participante)
class ParticipanteAdmin(admin.ModelAdmin):
    list_display = (
        "dni",
        "codigo_alumno",
        "apellidos_nombres",
        "tipo_tarifa",
        "monto_pagar",
        "estado_pago",
        "asistencia_manana",
        "asistencia_tarde",
        "fecha_registro",
    )
    list_filter = ("estado_pago", "es_matriculado", "asistencia_manana", "asistencia_tarde")
    search_fields = ("dni", "codigo_alumno", "apellidos_nombres")
    readonly_fields = ("qr_uuid", "qr_token", "fecha_registro", "actualizado_en")


@admin.register(LogAsistencia)
class LogAsistenciaAdmin(admin.ModelAdmin):
    list_display = ("fecha_hora", "bloque", "resultado", "participante", "escaneado_por", "modo_prueba")
    list_filter = ("resultado", "bloque", "modo_prueba")
    search_fields = ("participante__dni", "participante__apellidos_nombres", "mensaje")
    readonly_fields = (
        "participante",
        "token_recibido",
        "bloque",
        "resultado",
        "mensaje",
        "escaneado_por",
        "modo_prueba",
        "fecha_hora",
    )


@admin.register(ConfiguracionEvento)
class ConfiguracionEventoAdmin(admin.ModelAdmin):
    list_display = ("modo_prueba_activo", "turno_simulado", "actualizado_por", "actualizado_en")
