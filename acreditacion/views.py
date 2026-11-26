"""
Vistas públicas (registro en 4 pasos y recuperación de QR) y vistas administrativas
protegidas (dashboard en tiempo real, validación de pagos, escáner QR, padrón y Excel).
"""

import json
import re
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .forms import ImportarPadronForm, RegistroParticipanteForm
from .models import ConfiguracionEvento, LogAsistencia, PadronMatriculado, Participante
from .utils import (
    buscar_en_padron,
    calcular_tarifa_backend,
    generar_qr_base64,
    generar_qr_png_bytes,
    generar_reporte_excel,
    generar_token_qr,
    importar_padron_desde_archivo,
    obtener_bloque_horario_actual,
    verificar_firma_token_qr,
)


def _formatear_hora(dt):
    if not dt:
        return None
    return timezone.localtime(dt).strftime("%H:%M:%S")


def _formatear_fecha_hora(dt):
    if not dt:
        return None
    return timezone.localtime(dt).strftime("%d/%m/%Y %H:%M:%S")


def _parse_json_body(request):
    try:
        return json.loads(request.body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return None


# =====================================================================
# VISTAS PÚBLICAS (PARTICIPANTE / ESTUDIANTE SIN LOGIN)
# =====================================================================


@require_GET
def registro_pasos_view(request):
    """Vista pública principal con Stepper UI de 4 pasos y modal de recuperación QR."""
    context = {
        "congreso": settings.CONGRESO_CONFIG,
        "bloque": obtener_bloque_horario_actual(),
    }
    return render(request, "acreditacion/registro_pasos.html", context)


@require_GET
def api_consultar_padron(request):
    """
    Consulta en vivo si un DNI o Código pertenece al Padrón de Matriculados.
    Calcula la tarifa exclusivamente en backend (S/ 60.00 vs S/ 40.00).
    """
    dni = (request.GET.get("dni") or "").strip()
    codigo = (request.GET.get("codigo") or "").strip().upper()

    if dni and not re.fullmatch(r"\d{1,8}", dni):
        return JsonResponse({"ok": False, "error": "DNI debe ser numérico."}, status=400)

    # Verificar si ya existe inscripción activa con ese DNI
    if re.fullmatch(r"\d{8}", dni):
        existente = (
            Participante.objects.filter(dni=dni)
            .exclude(estado_pago=Participante.ESTADO_ANULADO)
            .first()
        )
        if existente:
            return JsonResponse(
                {
                    "ok": True,
                    "ya_registrado": True,
                    "estado_pago": existente.estado_pago,
                    "dni": existente.dni,
                    "apellidos_nombres": existente.apellidos_nombres,
                    "monto": f"{existente.monto_pagar:.2f}",
                }
            )

    tarifa = calcular_tarifa_backend(dni=dni, codigo_alumno=codigo)
    padron = tarifa["padron"]

    return JsonResponse(
        {
            "ok": True,
            "ya_registrado": False,
            "es_matriculado": tarifa["es_matriculado"],
            "tipo_tarifa": tarifa["tipo_tarifa"],
            "monto": f"{tarifa['monto']:.2f}",
            "padron": (
                {
                    "dni": padron.dni,
                    "codigo_alumno": padron.codigo_alumno,
                    "apellidos_nombres": padron.apellidos_nombres,
                }
                if padron
                else None
            ),
        }
    )


@require_POST
def api_registrar_participante(request):
    """
    Paso 1 -> Paso 2: Crea el registro en estado PENDIENTE_PAGO.
    El monto se calcula estrictamente en el backend consultando PadronMatriculado.
    """
    data = _parse_json_body(request)
    if data is None:
        return JsonResponse({"ok": False, "error": "Solicitud JSON inválida."}, status=400)

    form = RegistroParticipanteForm(data)
    if not form.is_valid():
        return JsonResponse(
            {"ok": False, "errors": form.errors, "error": "Verifique los campos ingresados."},
            status=400,
        )

    dni = form.cleaned_data["dni"]
    codigo_ingresado = form.cleaned_data["codigo_alumno"]
    apellidos_nombres = form.cleaned_data["apellidos_nombres"]

    # Validación estricta de tarifa en backend (nunca se confía en el frontend)
    tarifa = calcular_tarifa_backend(dni=dni, codigo_alumno=codigo_ingresado)
    padron = tarifa["padron"]

    # Evitar conflicto si el código ingresado pertenece a otro DNI del padrón
    if codigo_ingresado and not padron:
        otro_codigo = PadronMatriculado.objects.filter(codigo_alumno__iexact=codigo_ingresado).first()
        if otro_codigo and otro_codigo.dni != dni:
            return JsonResponse(
                {
                    "ok": False,
                    "error": "El código de alumno ingresado pertenece a otro DNI en el padrón oficial.",
                },
                status=400,
            )

    if padron and padron.dni != dni:
        return JsonResponse(
            {
                "ok": False,
                "error": "El código de alumno ingresado no corresponde al DNI proporcionado.",
            },
            status=400,
        )

    codigo_final = padron.codigo_alumno if padron else codigo_ingresado
    nombre_final = padron.apellidos_nombres.upper() if padron else apellidos_nombres

    try:
        with transaction.atomic():
            participante = Participante.objects.create(
                dni=dni,
                codigo_alumno=codigo_final,
                apellidos_nombres=nombre_final,
                es_matriculado=tarifa["es_matriculado"],
                tipo_tarifa=tarifa["tipo_tarifa"],
                monto_pagar=tarifa["monto"],
                estado_pago=Participante.ESTADO_PENDIENTE,
            )
    except IntegrityError:
        return JsonResponse(
            {
                "ok": False,
                "error": "Este DNI ya cuenta con una inscripción activa. Recupera tu estado con tu DNI.",
            },
            status=409,
        )

    return JsonResponse(
        {
            "ok": True,
            "participante": {
                "dni": participante.dni,
                "codigo_alumno": participante.codigo_alumno,
                "apellidos_nombres": participante.apellidos_nombres,
                "es_matriculado": participante.es_matriculado,
                "tipo_tarifa": participante.get_tipo_tarifa_display(),
                "monto_pagar": f"{participante.monto_pagar:.2f}",
                "estado_pago": participante.estado_pago,
            },
        },
        status=201,
    )


@require_GET
def api_estado_participante(request, dni: str):
    """
    Polling en tiempo real del Paso 2->3->4 y recuperación de Pase QR por DNI.
    """
    if not re.fullmatch(r"\d{8}", dni):
        return JsonResponse({"ok": False, "error": "El DNI debe tener 8 dígitos."}, status=400)

    participante = (
        Participante.objects.filter(dni=dni)
        .exclude(estado_pago=Participante.ESTADO_ANULADO)
        .first()
    )
    if not participante:
        return JsonResponse(
            {"ok": False, "error": "No se encontró ninguna inscripción activa para este DNI."},
            status=404,
        )

    payload = {
        "ok": True,
        "dni": participante.dni,
        "codigo_alumno": participante.codigo_alumno or "EXTERNO",
        "apellidos_nombres": participante.apellidos_nombres,
        "es_matriculado": participante.es_matriculado,
        "tipo_tarifa": participante.get_tipo_tarifa_display(),
        "monto_pagar": f"{participante.monto_pagar:.2f}",
        "estado_pago": participante.estado_pago,
        "asistencia_manana": participante.asistencia_manana,
        "hora_manana": _formatear_hora(participante.hora_manana),
        "asistencia_tarde": participante.asistencia_tarde,
        "hora_tarde": _formatear_hora(participante.hora_tarde),
        "estado_certificacion": participante.estado_certificacion,
    }

    if participante.estado_pago == Participante.ESTADO_PAGADO and participante.qr_token:
        payload["qr_base64"] = generar_qr_base64(participante.qr_token)
        payload["qr_download_url"] = reverse("descargar_qr_png", args=[participante.qr_token])
        payload["fecha_validacion_pago"] = _formatear_fecha_hora(participante.fecha_validacion_pago)

    return JsonResponse(payload)


@require_GET
def descargar_qr_png(request, token: str):
    """Descarga directa del Pase QR en formato PNG validando firma criptográfica."""
    es_valido, _ = verificar_firma_token_qr(token)
    if not es_valido:
        raise Http404("Pase QR no válido.")

    participante = Participante.objects.filter(
        qr_token=token, estado_pago=Participante.ESTADO_PAGADO
    ).first()
    if not participante:
        raise Http404("Pase QR no encontrado o no confirmado.")

    png_bytes = generar_qr_png_bytes(participante.qr_token)
    response = HttpResponse(png_bytes, content_type="image/png")
    response["Content-Disposition"] = f'attachment; filename="Pase_Congreso_{participante.dni}.png"'
    response["Cache-Control"] = "no-store"
    return response


# =====================================================================
# AUTENTICACIÓN DEL ADMINISTRADOR / STAFF
# =====================================================================


@require_http_methods(["GET", "POST"])
def login_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")

    form = AuthenticationForm(request, data=request.POST or None)
    if request.method == "POST" and form.is_valid():
        login(request, form.get_user())
        next_url = request.POST.get("next") or request.GET.get("next") or reverse("dashboard")
        # Evitar open redirect
        if not next_url.startswith("/") or next_url.startswith("//"):
            next_url = reverse("dashboard")
        return redirect(next_url)

    return render(
        request,
        "acreditacion/login.html",
        {"form": form, "congreso": settings.CONGRESO_CONFIG},
    )


@login_required
@require_POST
def logout_view(request):
    logout(request)
    return redirect("login")


# =====================================================================
# DASHBOARD, VALIDACIÓN DE PAGOS, PADRÓN Y REPORTES (@login_required)
# =====================================================================


@login_required
@require_GET
def dashboard_view(request):
    """Dashboard principal de administración con KPIs, cola de pagos y tabla de gestión."""
    config_evento = ConfiguracionEvento.obtener()
    context = {
        "congreso": settings.CONGRESO_CONFIG,
        "bloque": obtener_bloque_horario_actual(),
        "config_evento": config_evento,
        "turno_choices": ConfiguracionEvento.TURNO_SIMULADO_CHOICES,
        "total_padron": PadronMatriculado.objects.count(),
    }
    return render(request, "acreditacion/dashboard.html", context)


@login_required
@require_GET
def api_dashboard_data(request):
    """API en vivo para auto-refresco cada 5 segundos del dashboard con paginación."""
    q = (request.GET.get("q") or "").strip()
    filtro = (request.GET.get("estado") or "TODOS").strip().upper()

    try:
        page_num = max(1, int(request.GET.get("page", 1)))
    except ValueError:
        page_num = 1

    try:
        page_size = int(request.GET.get("page_size", 25))
        if page_size not in (10, 25, 50, 100):
            page_size = 25
    except ValueError:
        page_size = 25

    activos = Participante.objects.exclude(estado_pago=Participante.ESTADO_ANULADO)
    pagados = activos.filter(estado_pago=Participante.ESTADO_PAGADO)

    asist_manana = pagados.filter(asistencia_manana=True).count()
    asist_tarde = pagados.filter(asistencia_tarde=True).count()
    asist_completa = pagados.filter(asistencia_manana=True, asistencia_tarde=True).count()

    tabla_qs = activos.order_by("apellidos_nombres", "id")
    if q:
        tabla_qs = tabla_qs.filter(
            Q(dni__icontains=q)
            | Q(apellidos_nombres__icontains=q)
            | Q(codigo_alumno__icontains=q)
        )

    if filtro == "COMPLETA":
        tabla_qs = tabla_qs.filter(asistencia_manana=True, asistencia_tarde=True)
    elif filtro == "PARCIAL":
        tabla_qs = tabla_qs.filter(
            Q(asistencia_manana=True, asistencia_tarde=False)
            | Q(asistencia_manana=False, asistencia_tarde=True)
        )
    elif filtro == "AUSENTE":
        tabla_qs = tabla_qs.filter(asistencia_manana=False, asistencia_tarde=False)

    paginator = Paginator(tabla_qs, page_size)
    page_obj = paginator.get_page(page_num)
    start_index = page_obj.start_index() if paginator.count > 0 else 0

    bloque_info = obtener_bloque_horario_actual()

    return JsonResponse(
        {
            "ok": True,
            "kpis": {
                "total_participantes": activos.count(),
                "asist_manana": asist_manana,
                "asist_tarde": asist_tarde,
                "asist_completa": asist_completa,
            },
            "bloque_actual": {
                "bloque": bloque_info["bloque"],
                "etiqueta": bloque_info["etiqueta"],
                "hora_servidor": bloque_info["hora_servidor"].strftime("%H:%M:%S"),
                "fecha_servidor": bloque_info["hora_servidor"].strftime("%d/%m/%Y"),
                "modo_prueba": bloque_info["modo_prueba"],
            },
            "paginacion": {
                "page": page_obj.number,
                "num_pages": paginator.num_pages,
                "total_filtrado": paginator.count,
                "page_size": page_size,
                "has_previous": page_obj.has_previous(),
                "has_next": page_obj.has_next(),
                "start_index": start_index,
                "end_index": page_obj.end_index() if paginator.count > 0 else 0,
            },
            "participantes": [
                {
                    "numero": start_index + idx,
                    "id": p.id,
                    "dni": p.dni,
                    "codigo_alumno": p.codigo_alumno or "EXTERNO",
                    "apellidos_nombres": p.apellidos_nombres,
                    "asistencia_manana": p.asistencia_manana,
                    "hora_manana": _formatear_hora(p.hora_manana),
                    "asistencia_tarde": p.asistencia_tarde,
                    "hora_tarde": _formatear_hora(p.hora_tarde),
                    "estado_certificacion": p.estado_certificacion,
                    "tiene_qr": bool(p.qr_token and p.estado_pago == Participante.ESTADO_PAGADO),
                }
                for idx, p in enumerate(page_obj.object_list)
            ],
        }
    )


@login_required
@require_POST
def api_confirmar_pago(request, participante_id: int):
    """
    Confirma el pago en puerta de forma atómica, registra admin y hora exacta,
    y genera el token QR único firmado criptográficamente (UUIDv4 + HMAC-SHA256).
    """
    with transaction.atomic():
        participante = (
            Participante.objects.select_for_update().filter(pk=participante_id).first()
        )
        if not participante:
            return JsonResponse({"ok": False, "error": "Participante no encontrado."}, status=404)

        if participante.estado_pago == Participante.ESTADO_ANULADO:
            return JsonResponse(
                {"ok": False, "error": "No se puede confirmar un registro anulado."},
                status=400,
            )

        if participante.estado_pago == Participante.ESTADO_PAGADO and participante.qr_token:
            return JsonResponse(
                {
                    "ok": True,
                    "ya_confirmado": True,
                    "mensaje": f"El pago de {participante.apellidos_nombres} ya había sido confirmado.",
                }
            )

        uid, token = generar_token_qr()
        participante.qr_uuid = uid
        participante.qr_token = token
        participante.estado_pago = Participante.ESTADO_PAGADO
        participante.validado_por = request.user
        participante.fecha_validacion_pago = timezone.now()
        participante.save(
            update_fields=[
                "qr_uuid",
                "qr_token",
                "estado_pago",
                "validado_por",
                "fecha_validacion_pago",
                "actualizado_en",
            ]
        )

    return JsonResponse(
        {
            "ok": True,
            "mensaje": f"Pago confirmado (S/ {participante.monto_pagar:.2f}) y QR generado para {participante.apellidos_nombres}.",
            "participante_id": participante.id,
        }
    )


@login_required
@require_POST
def api_anular_registro(request, participante_id: int):
    """Anula un registro (invalida su QR y libera el DNI para nueva inscripción)."""
    with transaction.atomic():
        participante = (
            Participante.objects.select_for_update().filter(pk=participante_id).first()
        )
        if not participante:
            return JsonResponse({"ok": False, "error": "Participante no encontrado."}, status=404)

        participante.estado_pago = Participante.ESTADO_ANULADO
        participante.qr_token = None
        participante.qr_uuid = None
        participante.save(update_fields=["estado_pago", "qr_token", "qr_uuid", "actualizado_en"])

    return JsonResponse(
        {
            "ok": True,
            "mensaje": f"Registro de {participante.apellidos_nombres} (DNI {participante.dni}) anulado.",
        }
    )


@login_required
@require_GET
def api_admin_ver_qr(request, participante_id: int):
    """Permite al staff visualizar y descargar el QR de un participante pagado."""
    participante = Participante.objects.filter(
        pk=participante_id, estado_pago=Participante.ESTADO_PAGADO
    ).first()
    if not participante or not participante.qr_token:
        return JsonResponse(
            {"ok": False, "error": "El participante no cuenta con un pase QR activo."},
            status=404,
        )

    return JsonResponse(
        {
            "ok": True,
            "dni": participante.dni,
            "codigo_alumno": participante.codigo_alumno or "EXTERNO",
            "apellidos_nombres": participante.apellidos_nombres,
            "monto_pagar": f"{participante.monto_pagar:.2f}",
            "qr_base64": generar_qr_base64(participante.qr_token),
            "qr_download_url": reverse("descargar_qr_png", args=[participante.qr_token]),
            "asistencia_manana": participante.asistencia_manana,
            "asistencia_tarde": participante.asistencia_tarde,
        }
    )


@login_required
@require_POST
def api_importar_padron(request):
    """Importa masivamente el padrón de matriculados (.csv, .xlsx o .db) desde el dashboard."""
    form = ImportarPadronForm(request.POST, request.FILES)
    if not form.is_valid():
        err_msg = next(iter(form.errors.values()))[0] if form.errors else "Archivo inválido."
        return JsonResponse({"ok": False, "error": err_msg}, status=400)

    archivo = form.cleaned_data["archivo"]
    try:
        resumen = importar_padron_desde_archivo(archivo.name, archivo.read())
    except ValueError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    except Exception:
        return JsonResponse(
            {"ok": False, "error": "Ocurrió un error procesando el archivo de padrón."},
            status=500,
        )

    return JsonResponse(
        {
            "ok": True,
            "resumen": resumen,
            "total_padron": PadronMatriculado.objects.count(),
        }
    )


@login_required
@require_POST
def api_configurar_modo_prueba(request):
    """
    Activa o desactiva el Modo Prueba / Override de Horario.
    Requiere re-confirmar la contraseña del administrador actual por seguridad.
    """
    data = _parse_json_body(request)
    if data is None:
        return JsonResponse({"ok": False, "error": "Solicitud JSON inválida."}, status=400)

    password = data.get("password") or ""
    if not request.user.check_password(password):
        return JsonResponse(
            {"ok": False, "error": "Contraseña de administrador incorrecta."},
            status=403,
        )

    modo_activo = bool(data.get("modo_prueba_activo", False))
    turno_simulado = (data.get("turno_simulado") or ConfiguracionEvento.TURNO_NONE).strip().upper()
    valid_turnos = {c[0] for c in ConfiguracionEvento.TURNO_SIMULADO_CHOICES}
    if turno_simulado not in valid_turnos:
        return JsonResponse({"ok": False, "error": "Turno simulado inválido."}, status=400)

    if not modo_activo:
        turno_simulado = ConfiguracionEvento.TURNO_NONE

    with transaction.atomic():
        cfg = ConfiguracionEvento.obtener()
        cfg.modo_prueba_activo = modo_activo
        cfg.turno_simulado = turno_simulado
        cfg.actualizado_por = request.user
        cfg.save()

    bloque = obtener_bloque_horario_actual()
    return JsonResponse(
        {
            "ok": True,
            "mensaje": "Configuración de horario actualizada correctamente.",
            "bloque_actual": {
                "bloque": bloque["bloque"],
                "etiqueta": bloque["etiqueta"],
                "modo_prueba": bloque["modo_prueba"],
            },
        }
    )


@login_required
@require_GET
def exportar_excel_view(request):
    """Exporta el reporte oficial multi-hoja del congreso en formato .xlsx."""
    excel_bytes = generar_reporte_excel()
    ahora = timezone.localtime(timezone.now())
    filename = f"Reporte_Congreso_{ahora:%Y%m%d_%H%M}.xlsx"
    response = HttpResponse(
        excel_bytes,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["Cache-Control"] = "no-store"
    return response


# =====================================================================
# ESCÁNER QR DE DOBLE ASISTENCIA SIN SALIDAS (@login_required)
# =====================================================================


@login_required
@require_GET
def escaner_view(request):
    """Vista optimizada para celulares del staff en puerta con escáner html5-qrcode."""
    recientes = LogAsistencia.objects.select_related("participante", "escaneado_por")[:15]
    context = {
        "congreso": settings.CONGRESO_CONFIG,
        "bloque": obtener_bloque_horario_actual(),
        "recientes": recientes,
    }
    return render(request, "acreditacion/escaner.html", context)


@login_required
@require_POST
def api_escanear_qr(request):
    """
    Valida y registra la asistencia en puerta según la franja horaria del servidor
    (America/Lima) sin registro de salida y con bloqueo atómico anti-concurrencia.
    """
    data = _parse_json_body(request)
    if data is None:
        return JsonResponse({"ok": False, "error": "Solicitud JSON inválida."}, status=400)

    token = (data.get("token") or "").strip()
    bloque_info = obtener_bloque_horario_actual()
    bloque = bloque_info["bloque"]
    ahora = bloque_info["hora_servidor"]
    modo_prueba = bloque_info["modo_prueba"]

    # 1. Verificación criptográfica del token QR (anti-falsificación y anti-enumeración)
    es_valido, _ = verificar_firma_token_qr(token)
    if not es_valido:
        mensaje = "DENEGADO: CÓDIGO QR INVÁLIDO O NO RECONOCIDO POR EL SISTEMA."
        LogAsistencia.objects.create(
            participante=None,
            token_recibido=token[:160],
            bloque=bloque,
            resultado=LogAsistencia.RES_TOKEN_INVALIDO,
            mensaje=mensaje,
            escaneado_por=request.user,
            modo_prueba=modo_prueba,
        )
        return JsonResponse(
            {
                "ok": False,
                "nivel": "error",
                "titulo": "QR INVÁLIDO",
                "mensaje": mensaje,
                "bloque": bloque_info["etiqueta"],
                "hora": ahora.strftime("%H:%M:%S"),
            },
            status=400,
        )

    # 2. Transacción atómica con select_for_update() para evitar doble marcaje simultáneo
    with transaction.atomic():
        participante = (
            Participante.objects.select_for_update().filter(qr_token=token).first()
        )
        if not participante:
            mensaje = "DENEGADO: PASE QR NO ENCONTRADO O ANULADO."
            LogAsistencia.objects.create(
                participante=None,
                token_recibido=token[:160],
                bloque=bloque,
                resultado=LogAsistencia.RES_TOKEN_INVALIDO,
                mensaje=mensaje,
                escaneado_por=request.user,
                modo_prueba=modo_prueba,
            )
            return JsonResponse(
                {
                    "ok": False,
                    "nivel": "error",
                    "titulo": "PASE NO ENCONTRADO",
                    "mensaje": mensaje,
                    "bloque": bloque_info["etiqueta"],
                    "hora": ahora.strftime("%H:%M:%S"),
                },
                status=404,
            )

        info_participante = {
            "dni": participante.dni,
            "codigo_alumno": participante.codigo_alumno or "EXTERNO",
            "apellidos_nombres": participante.apellidos_nombres,
            "tarifa": "Matriculado (S/ 60)" if participante.es_matriculado else "Externo (S/ 40)",
            "asistencia_manana": participante.asistencia_manana,
            "asistencia_tarde": participante.asistencia_tarde,
        }

        if participante.estado_pago != Participante.ESTADO_PAGADO:
            mensaje = "DENEGADO: EL PAGO DE ESTE PARTICIPANTE NO ESTÁ CONFIRMADO."
            LogAsistencia.objects.create(
                participante=participante,
                token_recibido=token[:160],
                bloque=bloque,
                resultado=LogAsistencia.RES_NO_PAGADO,
                mensaje=mensaje,
                escaneado_por=request.user,
                modo_prueba=modo_prueba,
            )
            return JsonResponse(
                {
                    "ok": False,
                    "nivel": "error",
                    "titulo": "PAGO NO CONFIRMADO",
                    "mensaje": mensaje,
                    "participante": info_participante,
                    "bloque": bloque_info["etiqueta"],
                    "hora": ahora.strftime("%H:%M:%S"),
                },
                status=403,
            )

        # 3. Validación estricta de ventana horaria (hora del servidor)
        if bloque == LogAsistencia.BLOQUE_FUERA:
            mensaje = (
                "QR INACTIVO EN ESTE HORARIO. Horarios de marcación: "
                "Mañana (09:00 AM - 01:00 PM) | Tarde (03:00 PM - 07:00 PM)"
            )
            LogAsistencia.objects.create(
                participante=participante,
                token_recibido=token[:160],
                bloque=bloque,
                resultado=LogAsistencia.RES_FUERA_HORARIO,
                mensaje=mensaje,
                escaneado_por=request.user,
                modo_prueba=modo_prueba,
            )
            return JsonResponse(
                {
                    "ok": False,
                    "nivel": "warning",
                    "titulo": "QR INACTIVO EN ESTE HORARIO",
                    "mensaje": mensaje,
                    "participante": info_participante,
                    "bloque": bloque_info["etiqueta"],
                    "hora": ahora.strftime("%H:%M:%S"),
                },
                status=403,
            )

        # 4. Bloque 1: Turno Mañana (09:00 - 13:00)
        if bloque == LogAsistencia.BLOQUE_MANANA:
            if participante.asistencia_manana:
                mensaje = "DENEGADO: ESTE QR YA REGISTRÓ SU ASISTENCIA EN EL TURNO MAÑANA"
                LogAsistencia.objects.create(
                    participante=participante,
                    token_recibido=token[:160],
                    bloque=bloque,
                    resultado=LogAsistencia.RES_DUP_MANANA,
                    mensaje=mensaje,
                    escaneado_por=request.user,
                    modo_prueba=modo_prueba,
                )
                return JsonResponse(
                    {
                        "ok": False,
                        "nivel": "error",
                        "titulo": "ACCESO DENEGADO (ANTI-PASSBACK)",
                        "mensaje": mensaje,
                        "hora_previa": _formatear_hora(participante.hora_manana),
                        "participante": info_participante,
                        "bloque": bloque_info["etiqueta"],
                        "hora": ahora.strftime("%H:%M:%S"),
                    },
                    status=409,
                )

            participante.asistencia_manana = True
            participante.hora_manana = ahora
            participante.registrado_manana_por = request.user
            participante.save(
                update_fields=[
                    "asistencia_manana",
                    "hora_manana",
                    "registrado_manana_por",
                    "actualizado_en",
                ]
            )
            info_participante["asistencia_manana"] = True
            mensaje = "ASISTENCIA MAÑANA REGISTRADA (Bloque 1/2)"
            LogAsistencia.objects.create(
                participante=participante,
                token_recibido=token[:160],
                bloque=bloque,
                resultado=LogAsistencia.RES_OK_MANANA,
                mensaje=mensaje,
                escaneado_por=request.user,
                modo_prueba=modo_prueba,
            )
            return JsonResponse(
                {
                    "ok": True,
                    "nivel": "success",
                    "titulo": "ASISTENCIA REGISTRADA",
                    "mensaje": mensaje,
                    "participante": info_participante,
                    "bloque": bloque_info["etiqueta"],
                    "hora": ahora.strftime("%H:%M:%S"),
                }
            )

        # 5. Bloque 2: Turno Tarde (15:00 - 19:00)
        if bloque == LogAsistencia.BLOQUE_TARDE:
            if participante.asistencia_tarde:
                mensaje = "DENEGADO: ESTE QR YA REGISTRÓ SU ASISTENCIA EN EL TURNO TARDE"
                LogAsistencia.objects.create(
                    participante=participante,
                    token_recibido=token[:160],
                    bloque=bloque,
                    resultado=LogAsistencia.RES_DUP_TARDE,
                    mensaje=mensaje,
                    escaneado_por=request.user,
                    modo_prueba=modo_prueba,
                )
                return JsonResponse(
                    {
                        "ok": False,
                        "nivel": "error",
                        "titulo": "ACCESO DENEGADO (ANTI-PASSBACK)",
                        "mensaje": mensaje,
                        "hora_previa": _formatear_hora(participante.hora_tarde),
                        "participante": info_participante,
                        "bloque": bloque_info["etiqueta"],
                        "hora": ahora.strftime("%H:%M:%S"),
                    },
                    status=409,
                )

            participante.asistencia_tarde = True
            participante.hora_tarde = ahora
            participante.registrado_tarde_por = request.user
            participante.save(
                update_fields=[
                    "asistencia_tarde",
                    "hora_tarde",
                    "registrado_tarde_por",
                    "actualizado_en",
                ]
            )
            info_participante["asistencia_tarde"] = True
            mensaje = "ASISTENCIA TARDE REGISTRADA (Bloque 2/2)"
            LogAsistencia.objects.create(
                participante=participante,
                token_recibido=token[:160],
                bloque=bloque,
                resultado=LogAsistencia.RES_OK_TARDE,
                mensaje=mensaje,
                escaneado_por=request.user,
                modo_prueba=modo_prueba,
            )
            return JsonResponse(
                {
                    "ok": True,
                    "nivel": "success",
                    "titulo": "ASISTENCIA REGISTRADA",
                    "mensaje": mensaje,
                    "participante": info_participante,
                    "bloque": bloque_info["etiqueta"],
                    "hora": ahora.strftime("%H:%M:%S"),
                }
            )

    return JsonResponse({"ok": False, "error": "Estado horario no controlado."}, status=500)
