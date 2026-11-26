"""
Utilidades de seguridad criptográfica, reglas de negocio horarias,
generación de QR en memoria, importación de padrón y exportación a Excel.
"""

import base64
import csv
import hashlib
import hmac
import io
import os
import re
import sqlite3
import tempfile
import time as time_mod
import urllib.request
import uuid
from datetime import time
from decimal import Decimal

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
import qrcode
from qrcode.constants import ERROR_CORRECT_M
from PIL import Image, ImageDraw, ImageFont

from django.conf import settings
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from .models import ConfiguracionEvento, LogAsistencia, PadronMatriculado, Participante

QR_TOKEN_PREFIX = "CUNV1"
_LAST_SHEET_SYNC_TS = 0.0
_LAST_FORCED_SYNC_TS = 0.0
_LAST_FALTAN_CANCELAR: list[dict] = []


def sanitizar_celda_excel(valor):
    """Previene CSV/Excel Formula Injection (OWASP) escapando prefijos peligrosos."""
    if isinstance(valor, str) and valor and valor[0] in ("=", "+", "-", "@", "\t", "\r"):
        return f"'{valor}"
    return valor


def obtener_faltan_cancelar() -> list[dict]:
    """Retorna la última lista (solo lectura) de personas marcadas como 'falta cancelar/pagar' en el Sheet."""
    return list(_LAST_FALTAN_CANCELAR)


def _qr_signing_key() -> bytes:
    """Deriva una clave dedicada para firmar tokens QR usando HMAC-SHA256."""
    secret = f"{settings.SECRET_KEY}:congreso-qr-hmac-v1".encode("utf-8")
    return hashlib.sha256(secret).digest()


def uuid_deterministico_por_dni(dni: str) -> uuid.UUID:
    """
    Genera un UUIDv5 determinístico derivado del DNI y SECRET_KEY para que un mismo
    participante sincronizado desde Google Sheets conserve siempre el mismo QR
    incluso tras reinicios en frío de Vercel Serverless.
    """
    ns_bytes = hashlib.sha256(f"{settings.SECRET_KEY}:congreso-uuid-ns".encode("utf-8")).digest()[:16]
    namespace = uuid.UUID(bytes=ns_bytes)
    return uuid.uuid5(namespace, f"dni:{dni.strip()}")


def firmar_uuid_qr(uid: uuid.UUID) -> str:
    """Firma un UUIDv4 existente con HMAC-SHA256 en formato CUNV1.<uuid4_hex>.<hmac_32_hex>."""
    payload = f"{QR_TOKEN_PREFIX}.{uid.hex}"
    signature = hmac.new(
        _qr_signing_key(),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:32]
    return f"{payload}.{signature}"


def generar_token_qr() -> tuple[uuid.UUID, str]:
    """
    Genera un UUIDv4 no enumerable y lo firma criptográficamente con HMAC-SHA256.
    Formato: CUNV1.<uuid4_hex>.<hmac_32_hex>
    """
    uid = uuid.uuid4()
    return uid, firmar_uuid_qr(uid)


def verificar_firma_token_qr(token: str) -> tuple[bool, str | None]:
    """
    Verifica formato y firma criptográfica del token QR en tiempo constante.
    Retorna (es_valido, uuid_hex).
    """
    if not token or not isinstance(token, str):
        return False, None
    token_clean = token.strip()
    parts = token_clean.split(".")
    if len(parts) != 3:
        return False, None
    prefix, uid_hex, provided_sig = parts
    if prefix != QR_TOKEN_PREFIX:
        return False, None
    if not re.fullmatch(r"[0-9a-f]{32}", uid_hex):
        return False, None
    if not re.fullmatch(r"[0-9a-f]{32}", provided_sig):
        return False, None

    payload = f"{prefix}.{uid_hex}"
    expected_sig = hmac.new(
        _qr_signing_key(),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:32]

    if not hmac.compare_digest(expected_sig, provided_sig):
        return False, None
    return True, uid_hex


def generar_qr_png_bytes(token: str) -> bytes:
    """Genera la imagen PNG del código QR en memoria."""
    qr = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(token)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#0f172a", back_color="#ffffff")
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


def generar_qr_base64(token: str) -> str:
    """Retorna el código QR como Data URI base64 listo para etiquetas <img src='...'>."""
    png_bytes = generar_qr_png_bytes(token)
    b64 = base64.b64encode(png_bytes).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _cargar_fuente(size: int, bold: bool = False):
    """Carga una fuente TrueType compatible con tildes/ñ en Windows y Linux (Vercel)."""
    candidatas = (
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "C:/Windows/Fonts/segoeuib.ttf",
            "C:/Windows/Fonts/arialbd.ttf",
            "DejaVuSans-Bold.ttf",
        ]
        if bold
        else [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "C:/Windows/Fonts/segoeui.ttf",
            "C:/Windows/Fonts/arial.ttf",
            "DejaVuSans.ttf",
        ]
    )
    for ruta in candidatas:
        if os.path.exists(ruta):
            try:
                return ImageFont.truetype(ruta, size=size)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _envolver_texto(draw: ImageDraw.ImageDraw, texto: str, font, max_width: int) -> list[str]:
    palabras = texto.split()
    lineas = []
    actual = ""
    for p in palabras:
        prueba = f"{actual} {p}".strip()
        bbox = draw.textbbox((0, 0), prueba, font=font)
        if (bbox[2] - bbox[0]) <= max_width or not actual:
            actual = prueba
        else:
            lineas.append(actual)
            actual = p
    if actual:
        lineas.append(actual)
    return lineas


def generar_credencial_qr_png_bytes(participante: Participante) -> bytes:
    """
    Genera una credencial digital completa en PNG de alta resolución (900x1280)
    con la identidad visual de Congreso CIMA 2026 (guinda/carmesí y dorado),
    incluyendo el logotipo oficial, QR único, Nombres y Apellidos, DNI,
    Código de Estudiante y horarios oficiales.
    """
    W, H = 900, 1280
    burgundy_dark = (74, 5, 17)     # #4a0511
    burgundy = (122, 13, 30)        # #7a0d1e
    crimson = (163, 22, 44)         # #a3162c
    gold = (212, 146, 30)           # #d4921e
    gold_light = (245, 200, 92)     # #f5c85c
    slate_muted = (110, 88, 92)
    border_soft = (226, 204, 207)
    bg_cream = (252, 248, 247)
    white = (255, 255, 255)

    img = Image.new("RGB", (W, H), white)
    draw = ImageDraw.Draw(img)

    # Marco exterior guinda + marco interior dorado fino
    draw.rounded_rectangle([(18, 18), (W - 18, H - 18)], radius=32, fill=white, outline=burgundy, width=7)
    draw.rounded_rectangle([(30, 30), (W - 30, H - 30)], radius=24, fill=white, outline=gold, width=2)

    f_tag = _cargar_fuente(22, bold=True)
    f_name = _cargar_fuente(34, bold=True)
    f_label = _cargar_fuente(19, bold=True)
    f_val = _cargar_fuente(31, bold=True)
    f_foot_bold = _cargar_fuente(21, bold=True)
    f_foot = _cargar_fuente(18, bold=False)

    # Cabecera blanca con el logotipo oficial de Congreso CIMA 2026
    logo_path = settings.BASE_DIR / "acreditacion" / "assets" / "logo_cima.jpg"
    ribbon_top = 215
    if logo_path.exists():
        try:
            logo = Image.open(logo_path).convert("RGB")
            max_lw, max_lh = 560, 155
            logo.thumbnail((max_lw, max_lh), Image.Resampling.LANCZOS)
            lx = (W - logo.width) // 2
            ly = 48 + (max_lh - logo.height) // 2
            img.paste(logo, (lx, ly))
        except Exception:
            f_title = _cargar_fuente(42, bold=True)
            draw.text((W // 2, 125), "CONGRESO CIMA 2026", fill=burgundy, font=f_title, anchor="mm")
    else:
        f_title = _cargar_fuente(42, bold=True)
        draw.text((W // 2, 125), "CONGRESO CIMA 2026", fill=burgundy, font=f_title, anchor="mm")

    # Cinta institucional guinda con bordes dorados
    draw.rectangle([(31, ribbon_top), (W - 31, ribbon_top + 6)], fill=gold)
    draw.rectangle([(31, ribbon_top + 6), (W - 31, ribbon_top + 60)], fill=burgundy_dark)
    draw.rectangle([(31, ribbon_top + 60), (W - 31, ribbon_top + 65)], fill=gold)
    draw.text(
        (W // 2, ribbon_top + 33),
        "CREDENCIAL OFICIAL  ·  PASE DIGITAL DE ASISTENCIA",
        fill=gold_light,
        font=f_tag,
        anchor="mm",
    )

    # Generar código QR en guinda muy oscuro (#31040b) sobre blanco (alto contraste > 16:1)
    qr = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_M,
        box_size=12,
        border=3,
    )
    qr.add_data(participante.qr_token)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="#31040b", back_color="#ffffff").convert("RGB")
    qr_size = 480
    qr_img = qr_img.resize((qr_size, qr_size), Image.Resampling.NEAREST)

    qr_box_top = 318
    qr_box_left = (W - qr_size) // 2
    draw.rounded_rectangle(
        [(qr_box_left - 22, qr_box_top - 22), (qr_box_left + qr_size + 22, qr_box_top + qr_size + 22)],
        radius=24,
        fill=white,
        outline=gold,
        width=4,
    )
    img.paste(qr_img, (qr_box_left, qr_box_top))

    # Nombres y Apellidos
    y_cursor = qr_box_top + qr_size + 58
    lineas_nombre = _envolver_texto(draw, participante.apellidos_nombres.upper(), f_name, W - 120)
    for linea in lineas_nombre[:2]:
        draw.text((W // 2, y_cursor), linea, fill=burgundy_dark, font=f_name, anchor="mm")
        y_cursor += 44

    # Cajas de DNI y Código de Estudiante con acentos CIMA
    card_top = max(y_cursor + 18, 945)
    card_bottom = card_top + 112
    mid = W // 2

    draw.rounded_rectangle([(62, card_top), (mid - 16, card_bottom)], radius=18, fill=bg_cream, outline=border_soft, width=3)
    draw.text(((62 + mid - 16) // 2, card_top + 32), "DNI", fill=crimson, font=f_label, anchor="mm")
    draw.text(((62 + mid - 16) // 2, card_top + 76), participante.dni, fill=burgundy_dark, font=f_val, anchor="mm")

    codigo_mostrar = participante.codigo_alumno or "—"
    draw.rounded_rectangle([(mid + 16, card_top), (W - 62, card_bottom)], radius=18, fill=bg_cream, outline=border_soft, width=3)
    draw.text(((mid + 16 + W - 62) // 2, card_top + 32), "CÓDIGO DE ESTUDIANTE", fill=crimson, font=f_label, anchor="mm")
    draw.text(((mid + 16 + W - 62) // 2, card_top + 76), codigo_mostrar, fill=burgundy_dark, font=f_val, anchor="mm")

    # Banda inferior guinda con horarios en dorado
    foot_top = H - 150
    draw.rounded_rectangle([(31, foot_top), (W - 31, H - 31)], radius=20, fill=burgundy_dark)
    draw.rectangle([(31, foot_top), (W - 31, foot_top + 24)], fill=burgundy_dark)
    draw.rectangle([(31, foot_top), (W - 31, foot_top + 5)], fill=gold)
    draw.text(
        (W // 2, foot_top + 44),
        "Mañana: 09:00 AM - 01:00 PM   |   Tarde: 03:00 PM - 07:00 PM",
        fill=gold_light,
        font=f_foot_bold,
        anchor="mm",
    )
    draw.text(
        (W // 2, foot_top + 84),
        "Pase personal e intransferible · Válido para ambos turnos · Congreso CIMA 2026",
        fill=(242, 224, 227),
        font=f_foot,
        anchor="mm",
    )

    buffer = io.BytesIO()
    img.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def buscar_en_padron(dni: str = "", codigo_alumno: str = "") -> PadronMatriculado | None:
    """
    Busca en el padrón oficial por DNI y/o Código de Alumno (insensible a mayúsculas).
    """
    dni_clean = (dni or "").strip()
    cod_clean = (codigo_alumno or "").strip().upper()

    if dni_clean and re.fullmatch(r"\d{8}", dni_clean):
        match = PadronMatriculado.objects.filter(dni=dni_clean).first()
        if match:
            return match

    if cod_clean:
        match = PadronMatriculado.objects.filter(codigo_alumno__iexact=cod_clean).first()
        if match:
            return match

    return None


def calcular_tarifa_backend(dni: str = "", codigo_alumno: str = "") -> dict:
    """
    Calcula estrictamente en el backend la tarifa correspondiente según el padrón:
    - En padrón (matriculado): S/ 60.00
    - Fuera de padrón (externo): S/ 40.00
    """
    cfg = settings.CONGRESO_CONFIG
    matriculado = buscar_en_padron(dni=dni, codigo_alumno=codigo_alumno)
    if matriculado:
        return {
            "es_matriculado": True,
            "tipo_tarifa": Participante.TARIFA_MATRICULADO,
            "monto": Decimal(cfg["TARIFA_MATRICULADO"]),
            "padron": matriculado,
        }
    return {
        "es_matriculado": False,
        "tipo_tarifa": Participante.TARIFA_EXTERNO,
        "monto": Decimal(cfg["TARIFA_EXTERNO"]),
        "padron": None,
    }


def obtener_bloque_horario_actual(now=None) -> dict:
    """
    Evalúa estrictamente usando la hora local del servidor (America/Lima)
    en qué bloque horario nos encontramos. Respeta el Modo Prueba / Override si
    un administrador lo activó explícitamente.
    """
    ahora_local = timezone.localtime(now or timezone.now())
    config_evento = ConfiguracionEvento.obtener()

    if (
        config_evento.modo_prueba_activo
        and config_evento.turno_simulado != ConfiguracionEvento.TURNO_NONE
    ):
        bloque = config_evento.turno_simulado
        etiquetas = {
            ConfiguracionEvento.TURNO_MANANA: "Turno Mañana (09:00 AM - 01:00 PM) [MODO PRUEBA]",
            ConfiguracionEvento.TURNO_TARDE: "Turno Tarde (03:00 PM - 07:00 PM) [MODO PRUEBA]",
            ConfiguracionEvento.TURNO_FUERA: "Fuera de Horario [MODO PRUEBA]",
        }
        return {
            "bloque": bloque,
            "etiqueta": etiquetas.get(bloque, "Fuera de Horario"),
            "hora_servidor": ahora_local,
            "modo_prueba": True,
        }

    cfg = settings.CONGRESO_CONFIG
    t_actual = ahora_local.time()
    m_ini = time(*cfg["MANANA_INICIO"])
    m_fin = time(*cfg["MANANA_FIN"])
    t_ini = time(*cfg["TARDE_INICIO"])
    t_fin = time(*cfg["TARDE_FIN"])

    if m_ini <= t_actual <= m_fin:
        bloque = LogAsistencia.BLOQUE_MANANA
        etiqueta = "Turno Mañana (09:00 AM - 01:00 PM)"
    elif t_ini <= t_actual <= t_fin:
        bloque = LogAsistencia.BLOQUE_TARDE
        etiqueta = "Turno Tarde (03:00 PM - 07:00 PM)"
    else:
        bloque = LogAsistencia.BLOQUE_FUERA
        etiqueta = "Fuera de Horario de Marcación"

    return {
        "bloque": bloque,
        "etiqueta": etiqueta,
        "hora_servidor": ahora_local,
        "modo_prueba": False,
    }


def _normalizar_cabecera(col: str) -> str:
    txt = str(col or "").strip().lower()
    reemplazos = {
        "á": "a", "é": "e", "í": "i", "ó": "o", "ú": "u", "ñ": "n",
        " ": "_", "-": "_", ".": "",
    }
    for k, v in reemplazos.items():
        txt = txt.replace(k, v)
    return txt


def _extraer_filas_csv(file_bytes: bytes) -> list[dict]:
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = file_bytes.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("No se pudo decodificar el archivo CSV (use UTF-8 o Latin-1).")

    sample = text[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel

    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    return [row for row in reader]


def _extraer_filas_xlsx(file_bytes: bytes) -> list[dict]:
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    ws = wb.active
    rows_iter = ws.iter_rows(values_only=True)
    try:
        headers = next(rows_iter)
    except StopIteration:
        return []
    header_names = [str(h).strip() if h is not None else "" for h in headers]
    data = []
    for row in rows_iter:
        if all(cell is None or str(cell).strip() == "" for cell in row):
            continue
        data.append(dict(zip(header_names, row)))
    wb.close()
    return data


def _extraer_filas_sqlite(file_bytes: bytes) -> list[dict]:
    """Lee la primera tabla compatible (con columnas de dni y codigo) de un archivo .db/.sqlite3."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name

    try:
        conn = sqlite3.connect(f"file:{tmp_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        tables = [
            r[0]
            for r in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';"
            ).fetchall()
        ]
        if not tables:
            raise ValueError("El archivo SQLite no contiene tablas.")

        chosen_rows = []
        for table in tables:
            # Validar nombre de tabla de forma segura desde sqlite_master
            safe_table = table.replace('"', '""')
            cols = [c[1] for c in cur.execute(f'PRAGMA table_info("{safe_table}");').fetchall()]
            norm_cols = {_normalizar_cabecera(c) for c in cols}
            if any("dni" in c for c in norm_cols):
                rows = cur.execute(f'SELECT * FROM "{safe_table}";').fetchall()
                chosen_rows = [dict(r) for r in rows]
                break
        conn.close()
        if not chosen_rows and tables:
            raise ValueError(
                "No se encontró una tabla con columna DNI en la base de datos SQLite."
            )
        return chosen_rows
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def importar_padron_desde_archivo(filename: str, file_bytes: bytes) -> dict:
    """
    Importa masivamente alumnos matriculados desde .csv, .xlsx o .db/.sqlite3.
    Reconoce nombres de columna flexibles (dni, codigo, codigo_alumno, apellidos_nombres, nombre, etc.).
    """
    ext = os.path.splitext(filename.lower())[1]
    if ext == ".csv":
        raw_rows = _extraer_filas_csv(file_bytes)
    elif ext in (".xlsx", ".xlsm"):
        raw_rows = _extraer_filas_xlsx(file_bytes)
    elif ext in (".db", ".sqlite", ".sqlite3"):
        raw_rows = _extraer_filas_sqlite(file_bytes)
    else:
        raise ValueError("Formato no soportado. Use archivos .csv, .xlsx o .db/.sqlite3.")

    if not raw_rows:
        raise ValueError("El archivo está vacío o no contiene registros legibles.")

    creados = 0
    actualizados = 0
    omitidos = 0
    errores = []

    with transaction.atomic():
        for idx, raw in enumerate(raw_rows, start=2):
            norm = {_normalizar_cabecera(k): ("" if v is None else str(v).strip()) for k, v in raw.items()}

            dni = (
                norm.get("dni")
                or norm.get("documento")
                or norm.get("num_dni")
                or norm.get("nro_dni")
                or ""
            )
            # Limpiar posible ".0" de Excel cuando DNI se guarda como número
            if dni.endswith(".0"):
                dni = dni[:-2]
            dni = re.sub(r"\D", "", dni).zfill(8) if re.sub(r"\D", "", dni) else ""

            codigo = (
                norm.get("codigo_alumno")
                or norm.get("codigo")
                or norm.get("cod_alumno")
                or norm.get("codigo_estudiante")
                or norm.get("cod")
                or ""
            )
            if codigo.endswith(".0"):
                codigo = codigo[:-2]
            codigo = codigo.strip().upper()

            nombre = (
                norm.get("apellidos_nombres")
                or norm.get("nombres_apellidos")
                or norm.get("nombre_completo")
                or norm.get("alumno")
                or norm.get("estudiante")
                or ""
            )
            if not nombre:
                apellidos = norm.get("apellidos") or norm.get("apellido") or ""
                nombres = norm.get("nombres") or norm.get("nombre") or ""
                nombre = f"{apellidos} {nombres}".strip()

            if not re.fullmatch(r"\d{8}", dni) or not codigo or not nombre:
                omitidos += 1
                if len(errores) < 10:
                    errores.append(f"Fila {idx}: datos incompletos o DNI inválido (DNI='{dni}', Código='{codigo}').")
                continue

            # Actualizar si existe por DNI o por código
            existente = PadronMatriculado.objects.filter(Q(dni=dni) | Q(codigo_alumno__iexact=codigo)).first()
            if existente:
                existente.dni = dni
                existente.codigo_alumno = codigo
                existente.apellidos_nombres = nombre[:200]
                existente.save(update_fields=["dni", "codigo_alumno", "apellidos_nombres", "actualizado_en"])
                actualizados += 1
            else:
                PadronMatriculado.objects.create(
                    dni=dni,
                    codigo_alumno=codigo,
                    apellidos_nombres=nombre[:200],
                )
                creados += 1

    return {
        "total_leidos": len(raw_rows),
        "creados": creados,
        "actualizados": actualizados,
        "omitidos": omitidos,
        "errores": errores,
    }


def generar_reporte_excel() -> bytes:
    """
    Genera el Reporte Completo del Congreso en Excel (.xlsx) con 3 hojas:
    1. Resumen del Congreso
    2. Consolidado de Asistencia y Certificación
    3. Auditoría de Escaneos
    """
    wb = openpyxl.Workbook()

    # Estilos corporativos
    header_fill = PatternFill(start_color="0F172A", end_color="0F172A", fill_type="solid")
    sub_fill = PatternFill(start_color="E2E8F0", end_color="E2E8F0", fill_type="solid")
    ok_fill = PatternFill(start_color="DCFCE7", end_color="DCFCE7", fill_type="solid")
    warn_fill = PatternFill(start_color="FEF9C3", end_color="FEF9C3", fill_type="solid")
    err_fill = PatternFill(start_color="FEE2E2", end_color="FEE2E2", fill_type="solid")

    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    title_font = Font(name="Calibri", size=14, bold=True, color="0F172A")
    bold_font = Font(name="Calibri", size=11, bold=True, color="0F172A")
    normal_font = Font(name="Calibri", size=10, color="1E293B")
    thin_border = Border(
        left=Side(style="thin", color="CBD5E1"),
        right=Side(style="thin", color="CBD5E1"),
        top=Side(style="thin", color="CBD5E1"),
        bottom=Side(style="thin", color="CBD5E1"),
    )

    def _auto_fit(ws):
        for col in ws.columns:
            max_len = 0
            col_letter = get_column_letter(col[0].column)
            for cell in col:
                val = str(cell.value or "")
                if len(val) > max_len:
                    max_len = len(val)
            ws.column_dimensions[col_letter].width = min(max(max_len + 4, 14), 48)

    # Datos base
    participantes_activos = Participante.objects.exclude(estado_pago=Participante.ESTADO_ANULADO)
    pagados = participantes_activos.filter(estado_pago=Participante.ESTADO_PAGADO)
    pendientes = participantes_activos.filter(estado_pago=Participante.ESTADO_PENDIENTE)

    mat_pagados = pagados.filter(es_matriculado=True)
    ext_pagados = pagados.filter(es_matriculado=False)

    recaudado_total = pagados.aggregate(total=Sum("monto_pagar"))["total"] or Decimal("0.00")
    recaudado_mat = mat_pagados.aggregate(total=Sum("monto_pagar"))["total"] or Decimal("0.00")
    recaudado_ext = ext_pagados.aggregate(total=Sum("monto_pagar"))["total"] or Decimal("0.00")

    asist_manana = pagados.filter(asistencia_manana=True).count()
    asist_tarde = pagados.filter(asistencia_tarde=True).count()
    asist_completa = pagados.filter(asistencia_manana=True, asistencia_tarde=True).count()
    asist_parcial = pagados.filter(
        Q(asistencia_manana=True, asistencia_tarde=False)
        | Q(asistencia_manana=False, asistencia_tarde=True)
    ).count()
    ausentes = pagados.filter(asistencia_manana=False, asistencia_tarde=False).count()

    # ==========================================================
    # HOJA 1: Resumen del Congreso
    # ==========================================================
    ws1 = wb.active
    ws1.title = "Resumen del Congreso"
    ahora = timezone.localtime(timezone.now())

    ws1["A1"] = settings.CONGRESO_CONFIG["NOMBRE_EVENTO"]
    ws1["A1"].font = title_font
    ws1["A2"] = f"Reporte generado el {ahora:%d/%m/%Y %H:%M:%S} (Hora Lima)"
    ws1["A2"].font = normal_font

    secciones = [
        ("RESUMEN FINANCIERO", ["Indicador", "Cantidad", "Monto Recaudado (S/)"], [
            ("Alumnos Matriculados Pagados (S/ 60.00)", mat_pagados.count(), float(recaudado_mat)),
            ("Externos / Público General Pagados (S/ 40.00)", ext_pagados.count(), float(recaudado_ext)),
            ("TOTAL RECAUDADO CONFIRMADO", pagados.count(), float(recaudado_total)),
            ("Inscripciones Pendientes de Pago", pendientes.count(), 0.0),
        ]),
        ("ESTADÍSTICAS DE ASISTENCIA POR BLOQUE", ["Indicador", "Total Participantes", "% sobre Pagados"], [
            ("Turno Mañana (09:00 AM - 01:00 PM)", asist_manana, (asist_manana / pagados.count() * 100) if pagados.count() else 0),
            ("Turno Tarde (03:00 PM - 07:00 PM)", asist_tarde, (asist_tarde / pagados.count() * 100) if pagados.count() else 0),
            ("Asistencia Completa 100% (Ambos Bloques)", asist_completa, (asist_completa / pagados.count() * 100) if pagados.count() else 0),
            ("Asistencia Parcial 50% (Solo un Bloque)", asist_parcial, (asist_parcial / pagados.count() * 100) if pagados.count() else 0),
            ("Ausentes (Pagados sin asistencia)", ausentes, (ausentes / pagados.count() * 100) if pagados.count() else 0),
        ]),
    ]

    row_cursor = 4
    for sec_title, headers, rows in secciones:
        ws1.cell(row=row_cursor, column=1, value=sec_title).font = bold_font
        ws1.cell(row=row_cursor, column=1).fill = sub_fill
        ws1.merge_cells(start_row=row_cursor, start_column=1, end_row=row_cursor, end_column=3)
        row_cursor += 1

        for c_idx, h in enumerate(headers, start=1):
            cell = ws1.cell(row=row_cursor, column=c_idx, value=h)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border = thin_border
        row_cursor += 1

        for r in rows:
            for c_idx, val in enumerate(r, start=1):
                cell = ws1.cell(row=row_cursor, column=c_idx, value=val)
                cell.font = bold_font if "TOTAL" in str(r[0]) else normal_font
                cell.border = thin_border
                if c_idx == 3:
                    if sec_title.startswith("RESUMEN"):
                        cell.number_format = '"S/ "#,##0.00'
                    else:
                        cell.number_format = '0.0"%"'
                    cell.alignment = Alignment(horizontal="right")
                elif c_idx == 2:
                    cell.alignment = Alignment(horizontal="center")
            row_cursor += 1
        row_cursor += 1

    _auto_fit(ws1)

    # ==========================================================
    # HOJA 2: Consolidado de Asistencia y Certificación
    # ==========================================================
    ws2 = wb.create_sheet(title="Consolidado y Certificación")
    headers_2 = [
        "DNI",
        "Código de Alumno",
        "Apellidos y Nombres",
        "Categoría / Tarifa",
        "Monto (S/)",
        "Estado Pago",
        "Validado Por",
        "Hora Asistencia Mañana",
        "Hora Asistencia Tarde",
        "Estado Final Certificación",
    ]
    for c_idx, h in enumerate(headers_2, start=1):
        cell = ws2.cell(row=1, column=c_idx, value=h)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = thin_border

    for r_idx, p in enumerate(
        participantes_activos.select_related("validado_por").order_by("apellidos_nombres"),
        start=2,
    ):
        hora_m = timezone.localtime(p.hora_manana).strftime("%d/%m/%Y %H:%M:%S") if p.hora_manana else "—"
        hora_t = timezone.localtime(p.hora_tarde).strftime("%d/%m/%Y %H:%M:%S") if p.hora_tarde else "—"
        estado_cert = p.estado_certificacion if p.estado_pago == Participante.ESTADO_PAGADO else "Pendiente de Pago"

        vals = [
            p.dni,
            p.codigo_alumno or "EXTERNO",
            p.apellidos_nombres,
            "Matriculado (S/ 60)" if p.es_matriculado else "Externo (S/ 40)",
            float(p.monto_pagar),
            p.get_estado_pago_display(),
            p.validado_por.username if p.validado_por else "—",
            hora_m,
            hora_t,
            estado_cert,
        ]
        for c_idx, val in enumerate(vals, start=1):
            cell = ws2.cell(row=r_idx, column=c_idx, value=sanitizar_celda_excel(val))
            cell.font = normal_font
            cell.border = thin_border
            if c_idx == 5:
                cell.number_format = '"S/ "#,##0.00'
            if c_idx == 10:
                if "100%" in estado_cert:
                    cell.fill = ok_fill
                elif "50%" in estado_cert:
                    cell.fill = warn_fill
                else:
                    cell.fill = err_fill

    ws2.freeze_panes = "A2"
    _auto_fit(ws2)

    # ==========================================================
    # HOJA 3: Auditoría de Escaneos
    # ==========================================================
    ws3 = wb.create_sheet(title="Auditoría de Escaneos")
    headers_3 = [
        "Fecha y Hora (Lima)",
        "Bloque Horario",
        "Resultado",
        "DNI Participante",
        "Apellidos y Nombres",
        "Staff / Operador",
        "Modo Prueba",
        "Detalle / Mensaje",
    ]
    for c_idx, h in enumerate(headers_3, start=1):
        cell = ws3.cell(row=1, column=c_idx, value=h)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = thin_border

    logs = LogAsistencia.objects.select_related("participante", "escaneado_por").order_by("-fecha_hora")
    for r_idx, log in enumerate(logs, start=2):
        fh = timezone.localtime(log.fecha_hora).strftime("%d/%m/%Y %H:%M:%S")
        vals = [
            fh,
            log.get_bloque_display(),
            log.get_resultado_display(),
            log.participante.dni if log.participante else "—",
            log.participante.apellidos_nombres if log.participante else "Desconocido",
            log.escaneado_por.username if log.escaneado_por else "—",
            "SÍ" if log.modo_prueba else "NO",
            log.mensaje,
        ]
        for c_idx, val in enumerate(vals, start=1):
            cell = ws3.cell(row=r_idx, column=c_idx, value=sanitizar_celda_excel(val))
            cell.font = normal_font
            cell.border = thin_border
            if c_idx == 3:
                if log.resultado in (LogAsistencia.RES_OK_MANANA, LogAsistencia.RES_OK_TARDE):
                    cell.fill = ok_fill
                elif log.resultado == LogAsistencia.RES_FUERA_HORARIO:
                    cell.fill = warn_fill
                else:
                    cell.fill = err_fill

    ws3.freeze_panes = "A2"
    _auto_fit(ws3)

    output = io.BytesIO()
    wb.save(output)
    return output.getvalue()


def sincronizar_desde_google_sheet(force: bool = False) -> dict:
    """
    Lee en modo ESTRICTAMENTE SOLO LECTURA (HTTP GET CSV export) el Google Sheet configurado
    y agrega automáticamente nuevos participantes (excluyendo INVITADO EXTERNO, sin monto ni
    método de pago, y excluyendo a quienes tienen observación 'falta cancelar/pagar' hasta que cancelen).
    Nunca escribe ni modifica el Google Sheet.
    """
    global _LAST_SHEET_SYNC_TS, _LAST_FORCED_SYNC_TS, _LAST_FALTAN_CANCELAR

    url = getattr(settings, "GOOGLE_SHEET_CSV_URL", "")
    if not url:
        return {"ok": False, "skipped": True, "error": "GOOGLE_SHEET_CSV_URL no configurado."}

    intervalo = getattr(settings, "GOOGLE_SHEET_SYNC_INTERVAL", 30)
    ahora_ts = time_mod.monotonic()
    if force and _LAST_FORCED_SYNC_TS and (ahora_ts - _LAST_FORCED_SYNC_TS) < 15:
        return {
            "ok": True,
            "skipped": True,
            "creados": 0,
            "actualizados": 0,
            "faltan_cancelar": list(_LAST_FALTAN_CANCELAR),
        }
    if not force and _LAST_SHEET_SYNC_TS and (ahora_ts - _LAST_SHEET_SYNC_TS) < intervalo:
        return {
            "ok": True,
            "skipped": True,
            "creados": 0,
            "actualizados": 0,
            "faltan_cancelar": list(_LAST_FALTAN_CANCELAR),
        }

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "CongresoAcreditacion/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw_bytes = resp.read()
        text = raw_bytes.decode("utf-8-sig", errors="replace")
    except Exception as exc:
        return {"ok": False, "skipped": False, "error": f"No se pudo leer Google Sheet: {exc}"}

    rows = list(csv.reader(io.StringIO(text)))
    idx_nombre = None
    idx_dni = None
    idx_codigo = None
    header_row_idx = None

    for i, row in enumerate(rows[:30]):
        norm_cells = [_normalizar_cabecera(c) for c in row]
        if "dni" in norm_cells and any(
            k in norm_cells for k in ("nombres_y_apellidos", "apellidos_y_nombres", "apellidos_nombres")
        ):
            header_row_idx = i
            idx_dni = norm_cells.index("dni")
            for k in ("nombres_y_apellidos", "apellidos_y_nombres", "apellidos_nombres"):
                if k in norm_cells:
                    idx_nombre = norm_cells.index(k)
                    break
            for k in ("codigo_de_estudiante", "codigo_alumno", "codigo", "codigo_estudiante"):
                if k in norm_cells:
                    idx_codigo = norm_cells.index(k)
                    break
            break

    if header_row_idx is None or idx_dni is None or idx_nombre is None:
        return {"ok": False, "skipped": False, "error": "No se encontraron cabeceras DNI / Nombres en el Sheet."}

    creados = 0
    actualizados = 0
    excluidos_externos = 0
    excluidos_docentes = 0
    omitidos_dni = 0
    faltan_cancelar = []
    dnis_excluidos = set()
    ahora_dt = timezone.now()

    with transaction.atomic():
        existentes_por_dni = {
            p.dni: p
            for p in Participante.objects.exclude(estado_pago=Participante.ESTADO_ANULADO)
        }
        vistos = set()

        for row in rows[header_row_idx + 1 :]:
            if len(row) <= max(idx_dni, idx_nombre):
                continue

            nombre = re.sub(r"\s+", " ", (row[idx_nombre] or "").strip())
            dni = (row[idx_dni] or "").strip()
            codigo = (
                (row[idx_codigo] or "").strip().upper()
                if idx_codigo is not None and len(row) > idx_codigo
                else ""
            )

            if not nombre and not dni:
                continue

            if "EXTERNO" in codigo or "EXTERNO" in dni.upper():
                excluidos_externos += 1
                if re.fullmatch(r"\d{8}", dni):
                    dnis_excluidos.add(dni)
                continue

            if "DOCENTE" in codigo or "DOCENTE" in dni.upper():
                excluidos_docentes += 1
                if re.fullmatch(r"\d{8}", dni):
                    dnis_excluidos.add(dni)
                continue

            # Detectar si tiene observación de "falta cancelar" / "falta pagar" / "pendiente"
            obs_text = " ".join(
                c.strip() for c in row[max(idx_dni, idx_nombre, idx_codigo or 0) + 1 :] if c and c.strip()
            )
            obs_lower = obs_text.lower()
            falta_pago = any(
                kw in obs_lower for kw in ("falta cancelar", "falta pagar", "falta", "debe", "pendiente")
            )

            if falta_pago:
                faltan_cancelar.append(
                    {
                        "apellidos_nombres": nombre.upper(),
                        "dni": dni or "—",
                        "codigo_alumno": codigo or "—",
                        "observacion": (
                            next(
                                (
                                    c.strip()
                                    for c in row[max(idx_dni, idx_nombre, idx_codigo or 0) + 1 :]
                                    if c and any(k in c.lower() for k in ("falta", "debe", "pendiente"))
                                ),
                                "Falta cancelar",
                            )
                        ),
                    }
                )
                if re.fullmatch(r"\d{8}", dni):
                    dnis_excluidos.add(dni)
                continue

            if not re.fullmatch(r"\d{8}", dni):
                omitidos_dni += 1
                continue

            if dni in vistos:
                continue
            vistos.add(dni)

            nombre_upper = nombre.upper()[:200]
            codigo_clean = codigo[:32]

            existente = existentes_por_dni.get(dni)
            if existente:
                cambios = []
                if existente.apellidos_nombres != nombre_upper:
                    existente.apellidos_nombres = nombre_upper
                    cambios.append("apellidos_nombres")
                if codigo_clean and existente.codigo_alumno != codigo_clean:
                    existente.codigo_alumno = codigo_clean
                    cambios.append("codigo_alumno")
                if cambios:
                    existente.save(update_fields=cambios + ["actualizado_en"])
                    actualizados += 1
            else:
                uid = uuid_deterministico_por_dni(dni)
                token = firmar_uuid_qr(uid)
                Participante.objects.create(
                    dni=dni,
                    codigo_alumno=codigo_clean,
                    apellidos_nombres=nombre_upper,
                    es_matriculado=True,
                    tipo_tarifa=Participante.TARIFA_MATRICULADO,
                    monto_pagar=Decimal("0.00"),
                    estado_pago=Participante.ESTADO_PAGADO,
                    fecha_validacion_pago=ahora_dt,
                    qr_uuid=uid,
                    qr_token=token,
                )
                creados += 1

        # Desactivar registros excluidos (docentes, externos o quienes faltan cancelar)
        if dnis_excluidos:
            Participante.objects.filter(
                dni__in=dnis_excluidos,
                asistencia_manana=False,
                asistencia_tarde=False,
            ).exclude(estado_pago=Participante.ESTADO_ANULADO).update(
                estado_pago=Participante.ESTADO_ANULADO,
                qr_token=None,
                qr_uuid=None,
            )

    _LAST_FALTAN_CANCELAR = faltan_cancelar
    now_mono = time_mod.monotonic()
    _LAST_SHEET_SYNC_TS = now_mono
    if force:
        _LAST_FORCED_SYNC_TS = now_mono
    return {
        "ok": True,
        "skipped": False,
        "creados": creados,
        "actualizados": actualizados,
        "excluidos_externos": excluidos_externos,
        "excluidos_docentes": excluidos_docentes,
        "omitidos_dni": omitidos_dni,
        "faltan_cancelar": faltan_cancelar,
    }
