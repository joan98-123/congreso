from decimal import Decimal
from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q

dni_validator = RegexValidator(
    regex=r"^\d{8}$",
    message="El DNI debe contener exactamente 8 dígitos numéricos.",
)


class PadronMatriculado(models.Model):
    """Padrón oficial de alumnos matriculados para asignación automática de tarifa (S/ 60.00)."""

    codigo_alumno = models.CharField(
        max_length=32,
        unique=True,
        db_index=True,
        verbose_name="Código de Alumno",
    )
    dni = models.CharField(
        max_length=8,
        unique=True,
        db_index=True,
        validators=[dni_validator],
        verbose_name="DNI",
    )
    apellidos_nombres = models.CharField(
        max_length=200,
        verbose_name="Apellidos y Nombres",
    )
    creado_en = models.DateTimeField(auto_now_add=True)
    actualizado_en = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Alumno Matriculado (Padrón)"
        verbose_name_plural = "Padrón de Alumnos Matriculados"
        ordering = ["apellidos_nombres"]

    def __str__(self):
        return f"{self.codigo_alumno} - {self.apellidos_nombres} ({self.dni})"


class ConfiguracionEvento(models.Model):
    """
    Configuración singleton (pk=1) para controlar el Modo Prueba / Override de Horario
    protegido por contraseña de administrador.
    """

    TURNO_NONE = "NONE"
    TURNO_MANANA = "MANANA"
    TURNO_TARDE = "TARDE"
    TURNO_FUERA = "FUERA"

    TURNO_SIMULADO_CHOICES = [
        (TURNO_NONE, "Horario Real del Servidor (Producción)"),
        (TURNO_MANANA, "Override: Turno Mañana (09:00 - 13:00)"),
        (TURNO_TARDE, "Override: Turno Tarde (15:00 - 19:00)"),
        (TURNO_FUERA, "Override: Fuera de Horario"),
    ]

    modo_prueba_activo = models.BooleanField(default=False)
    turno_simulado = models.CharField(
        max_length=16,
        choices=TURNO_SIMULADO_CHOICES,
        default=TURNO_NONE,
    )
    actualizado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="configuraciones_evento",
    )
    actualizado_en = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Configuración del Evento"
        verbose_name_plural = "Configuración del Evento"

    @classmethod
    def obtener(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        if self.modo_prueba_activo and self.turno_simulado != self.TURNO_NONE:
            return f"Modo Prueba Activo ({self.get_turno_simulado_display()})"
        return "Horario Real del Servidor"


class Participante(models.Model):
    """Registro de participante inscrito en puerta con pase QR único y doble asistencia."""

    ESTADO_PENDIENTE = "PENDIENTE_PAGO"
    ESTADO_PAGADO = "PAGADO"
    ESTADO_ANULADO = "ANULADO"

    ESTADO_PAGO_CHOICES = [
        (ESTADO_PENDIENTE, "Pendiente de Pago"),
        (ESTADO_PAGADO, "Pagado"),
        (ESTADO_ANULADO, "Anulado"),
    ]

    TARIFA_MATRICULADO = "MATRICULADO"
    TARIFA_EXTERNO = "EXTERNO"

    TIPO_TARIFA_CHOICES = [
        (TARIFA_MATRICULADO, "Alumno Matriculado (S/ 60.00)"),
        (TARIFA_EXTERNO, "Externo / Público General (S/ 40.00)"),
    ]

    dni = models.CharField(
        max_length=8,
        db_index=True,
        validators=[dni_validator],
        verbose_name="DNI",
    )
    codigo_alumno = models.CharField(
        max_length=32,
        blank=True,
        default="",
        db_index=True,
        verbose_name="Código de Alumno",
    )
    apellidos_nombres = models.CharField(
        max_length=200,
        verbose_name="Apellidos y Nombres",
    )
    es_matriculado = models.BooleanField(default=False, verbose_name="Es Matriculado")
    tipo_tarifa = models.CharField(
        max_length=20,
        choices=TIPO_TARIFA_CHOICES,
        default=TARIFA_EXTERNO,
        verbose_name="Tipo de Tarifa",
    )
    monto_pagar = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        default=Decimal("40.00"),
        verbose_name="Monto (S/)",
    )
    estado_pago = models.CharField(
        max_length=20,
        choices=ESTADO_PAGO_CHOICES,
        default=ESTADO_PENDIENTE,
        db_index=True,
        verbose_name="Estado de Pago",
    )
    validado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="pagos_validados",
        verbose_name="Pago Validado Por",
    )
    fecha_validacion_pago = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Fecha de Validación de Pago",
    )
    qr_uuid = models.UUIDField(
        null=True,
        blank=True,
        unique=True,
        editable=False,
    )
    qr_token = models.CharField(
        max_length=160,
        null=True,
        blank=True,
        unique=True,
        editable=False,
        db_index=True,
    )
    asistencia_manana = models.BooleanField(
        default=False,
        verbose_name="Asistencia Mañana (09:00-13:00)",
    )
    hora_manana = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Hora Asistencia Mañana",
    )
    registrado_manana_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="asistencias_manana_registradas",
    )
    asistencia_tarde = models.BooleanField(
        default=False,
        verbose_name="Asistencia Tarde (15:00-19:00)",
    )
    hora_tarde = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Hora Asistencia Tarde",
    )
    registrado_tarde_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="asistencias_tarde_registradas",
    )
    fecha_registro = models.DateTimeField(auto_now_add=True, db_index=True)
    actualizado_en = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Participante"
        verbose_name_plural = "Participantes"
        ordering = ["-fecha_registro"]
        constraints = [
            models.UniqueConstraint(
                fields=["dni"],
                condition=~Q(estado_pago="ANULADO"),
                name="unique_active_dni_participante",
            )
        ]

    @property
    def estado_certificacion(self) -> str:
        if self.asistencia_manana and self.asistencia_tarde:
            return "Asistencia Completa 100%"
        if self.asistencia_manana or self.asistencia_tarde:
            return "Asistencia Parcial 50%"
        return "Ausente"

    def __str__(self):
        return f"{self.apellidos_nombres} (DNI: {self.dni}) - {self.estado_pago}"


class LogAsistencia(models.Model):
    """Auditoría cronológica inmutable de cada intento de escaneo QR en puerta."""

    RES_OK_MANANA = "OK_MANANA"
    RES_OK_TARDE = "OK_TARDE"
    RES_DUP_MANANA = "DENEGADO_DUPLICADO_MANANA"
    RES_DUP_TARDE = "DENEGADO_DUPLICADO_TARDE"
    RES_FUERA_HORARIO = "DENEGADO_FUERA_HORARIO"
    RES_NO_PAGADO = "DENEGADO_NO_PAGADO"
    RES_TOKEN_INVALIDO = "DENEGADO_TOKEN_INVALIDO"

    RESULTADO_CHOICES = [
        (RES_OK_MANANA, "Asistencia Mañana Registrada (Bloque 1/2)"),
        (RES_OK_TARDE, "Asistencia Tarde Registrada (Bloque 2/2)"),
        (RES_DUP_MANANA, "Denegado: Duplicado Turno Mañana"),
        (RES_DUP_TARDE, "Denegado: Duplicado Turno Tarde"),
        (RES_FUERA_HORARIO, "Denegado: Fuera de Horario"),
        (RES_NO_PAGADO, "Denegado: Pago No Confirmado"),
        (RES_TOKEN_INVALIDO, "Denegado: QR Inválido o Alterado"),
    ]

    BLOQUE_MANANA = "MANANA"
    BLOQUE_TARDE = "TARDE"
    BLOQUE_FUERA = "FUERA"

    BLOQUE_CHOICES = [
        (BLOQUE_MANANA, "Turno Mañana (09:00-13:00)"),
        (BLOQUE_TARDE, "Turno Tarde (15:00-19:00)"),
        (BLOQUE_FUERA, "Fuera de Horario"),
    ]

    participante = models.ForeignKey(
        Participante,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="logs_asistencia",
    )
    token_recibido = models.CharField(max_length=160, blank=True, default="")
    bloque = models.CharField(max_length=16, choices=BLOQUE_CHOICES, default=BLOQUE_FUERA)
    resultado = models.CharField(max_length=40, choices=RESULTADO_CHOICES, db_index=True)
    mensaje = models.CharField(max_length=255)
    escaneado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="escaneos_realizados",
    )
    modo_prueba = models.BooleanField(default=False)
    fecha_hora = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "Log de Auditoría de Escaneo"
        verbose_name_plural = "Auditoría de Escaneos"
        ordering = ["-fecha_hora"]

    def __str__(self):
        return f"[{self.fecha_hora:%Y-%m-%d %H:%M:%S}] {self.resultado} - {self.mensaje[:60]}"
