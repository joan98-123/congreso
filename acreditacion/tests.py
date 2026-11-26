import io
import json
import sqlite3
import tempfile
from datetime import datetime
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

import openpyxl
from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from .models import ConfiguracionEvento, LogAsistencia, PadronMatriculado, Participante
from .utils import (
    importar_padron_desde_archivo,
    sincronizar_desde_google_sheet,
    verificar_firma_token_qr,
)

LIMA_TZ = ZoneInfo("America/Lima")


@override_settings(GOOGLE_SHEET_CSV_URL="")
class CongresoSistemaTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin_user = User.objects.create_superuser(
            username="admin_staff",
            email="admin@congreso.edu.pe",
            password="PasswordSeguro2026!",
        )
        self.padron = PadronMatriculado.objects.create(
            codigo_alumno="20220145",
            dni="71234567",
            apellidos_nombres="QUISPE TORRES, MARIA ELENA",
        )

    def _login_admin(self):
        self.client.force_login(self.admin_user)

    def test_tarifa_automatica_backend_matriculado_vs_externo(self):
        # 1. Matriculado (aunque el cliente intente manipular monto_pagar=1.00)
        res = self.client.post(
            reverse("api_registrar_participante"),
            data=json.dumps(
                {
                    "dni": "71234567",
                    "codigo_alumno": "20220145",
                    "apellidos_nombres": "QUISPE TORRES, MARIA ELENA",
                    "monto_pagar": "1.00",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 201)
        p_mat = Participante.objects.get(dni="71234567")
        self.assertTrue(p_mat.es_matriculado)
        self.assertEqual(p_mat.monto_pagar, Decimal("60.00"))
        self.assertEqual(p_mat.estado_pago, Participante.ESTADO_PENDIENTE)

        # 2. Externo (no está en padrón)
        res2 = self.client.post(
            reverse("api_registrar_participante"),
            data=json.dumps(
                {
                    "dni": "79998888",
                    "codigo_alumno": "",
                    "apellidos_nombres": "LOPEZ RUIZ, CARLOS ALBERTO",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(res2.status_code, 201)
        p_ext = Participante.objects.get(dni="79998888")
        self.assertFalse(p_ext.es_matriculado)
        self.assertEqual(p_ext.monto_pagar, Decimal("40.00"))

        # 3. DNI duplicado activo debe fallar
        res_dup = self.client.post(
            reverse("api_registrar_participante"),
            data=json.dumps(
                {
                    "dni": "79998888",
                    "codigo_alumno": "",
                    "apellidos_nombres": "OTRO NOMBRE",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(res_dup.status_code, 400)

    def test_proteccion_rutas_admin_sin_login(self):
        rutas_get = [
            reverse("dashboard"),
            reverse("escaner"),
            reverse("api_dashboard_data"),
            reverse("exportar_excel"),
        ]
        for url in rutas_get:
            res = self.client.get(url)
            self.assertEqual(res.status_code, 302)
            self.assertIn("/login/", res.url)

        res_scan = self.client.post(
            reverse("api_escanear_qr"),
            data=json.dumps({"token": "CUNV1.fake.fake"}),
            content_type="application/json",
        )
        self.assertEqual(res_scan.status_code, 302)

    def test_flujo_completo_pago_qr_doble_asistencia_y_horarios(self):
        # Registrar participante
        self.client.post(
            reverse("api_registrar_participante"),
            data=json.dumps(
                {
                    "dni": "71234567",
                    "codigo_alumno": "20220145",
                    "apellidos_nombres": "QUISPE TORRES, MARIA ELENA",
                }
            ),
            content_type="application/json",
        )
        p = Participante.objects.get(dni="71234567")

        # Admin confirma pago -> genera QR firmado criptográficamente
        self._login_admin()
        res_pago = self.client.post(reverse("api_confirmar_pago", args=[p.id]))
        self.assertEqual(res_pago.status_code, 200)
        p.refresh_from_db()
        self.assertEqual(p.estado_pago, Participante.ESTADO_PAGADO)
        self.assertEqual(p.validado_por, self.admin_user)
        self.assertIsNotNone(p.qr_token)
        valido, _ = verificar_firma_token_qr(p.qr_token)
        self.assertTrue(valido)

        # Descarga pública de QR PNG
        res_png = self.client.get(reverse("descargar_qr_png", args=[p.qr_token]))
        self.assertEqual(res_png.status_code, 200)
        self.assertEqual(res_png["Content-Type"], "image/png")

        # Token alterado debe ser rechazado
        tampered = p.qr_token[:-4] + "0000"
        res_bad = self.client.post(
            reverse("api_escanear_qr"),
            data=json.dumps({"token": tampered}),
            content_type="application/json",
        )
        self.assertEqual(res_bad.status_code, 400)

        # 1) Escaneo antes de las 09:00 AM (08:30) -> Fuera de horario
        with patch("acreditacion.utils.timezone.now", return_value=datetime(2026, 11, 25, 8, 30, tzinfo=LIMA_TZ)):
            r = self.client.post(
                reverse("api_escanear_qr"),
                data=json.dumps({"token": p.qr_token}),
                content_type="application/json",
            )
            self.assertEqual(r.status_code, 403)
            self.assertIn("QR INACTIVO EN ESTE HORARIO", r.json()["mensaje"])

        # 2) Escaneo Turno Mañana (10:15 AM) -> Éxito Bloque 1/2
        with patch("acreditacion.utils.timezone.now", return_value=datetime(2026, 11, 25, 10, 15, tzinfo=LIMA_TZ)):
            r = self.client.post(
                reverse("api_escanear_qr"),
                data=json.dumps({"token": p.qr_token}),
                content_type="application/json",
            )
            self.assertEqual(r.status_code, 200)
            self.assertIn("ASISTENCIA MAÑANA REGISTRADA (Bloque 1/2)", r.json()["mensaje"])

        # 3) Segundo escaneo en Turno Mañana (11:00 AM) -> Anti-passback
        with patch("acreditacion.utils.timezone.now", return_value=datetime(2026, 11, 25, 11, 0, tzinfo=LIMA_TZ)):
            r = self.client.post(
                reverse("api_escanear_qr"),
                data=json.dumps({"token": p.qr_token}),
                content_type="application/json",
            )
            self.assertEqual(r.status_code, 409)
            self.assertIn("YA REGISTRÓ SU ASISTENCIA EN EL TURNO MAÑANA", r.json()["mensaje"])

        # 4) Intermedio (01:30 PM) -> Fuera de horario
        with patch("acreditacion.utils.timezone.now", return_value=datetime(2026, 11, 25, 13, 30, tzinfo=LIMA_TZ)):
            r = self.client.post(
                reverse("api_escanear_qr"),
                data=json.dumps({"token": p.qr_token}),
                content_type="application/json",
            )
            self.assertEqual(r.status_code, 403)

        # 5) Turno Tarde (03:45 PM) -> Éxito Bloque 2/2
        with patch("acreditacion.utils.timezone.now", return_value=datetime(2026, 11, 25, 15, 45, tzinfo=LIMA_TZ)):
            r = self.client.post(
                reverse("api_escanear_qr"),
                data=json.dumps({"token": p.qr_token}),
                content_type="application/json",
            )
            self.assertEqual(r.status_code, 200)
            self.assertIn("ASISTENCIA TARDE REGISTRADA (Bloque 2/2)", r.json()["mensaje"])

        # 6) Segundo escaneo Turno Tarde (04:10 PM) -> Anti-passback
        with patch("acreditacion.utils.timezone.now", return_value=datetime(2026, 11, 25, 16, 10, tzinfo=LIMA_TZ)):
            r = self.client.post(
                reverse("api_escanear_qr"),
                data=json.dumps({"token": p.qr_token}),
                content_type="application/json",
            )
            self.assertEqual(r.status_code, 409)
            self.assertIn("YA REGISTRÓ SU ASISTENCIA EN EL TURNO TARDE", r.json()["mensaje"])

        p.refresh_from_db()
        self.assertTrue(p.asistencia_manana)
        self.assertTrue(p.asistencia_tarde)
        self.assertEqual(p.estado_certificacion, "Asistencia Completa 100%")
        self.assertEqual(LogAsistencia.objects.count(), 7)

    def test_modo_prueba_protegido_por_contrasena(self):
        self._login_admin()
        # Contraseña incorrecta -> 403
        r_bad = self.client.post(
            reverse("api_configurar_modo_prueba"),
            data=json.dumps(
                {
                    "modo_prueba_activo": True,
                    "turno_simulado": ConfiguracionEvento.TURNO_MANANA,
                    "password": "incorrecta",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(r_bad.status_code, 403)

        # Contraseña correcta -> 200
        r_ok = self.client.post(
            reverse("api_configurar_modo_prueba"),
            data=json.dumps(
                {
                    "modo_prueba_activo": True,
                    "turno_simulado": ConfiguracionEvento.TURNO_MANANA,
                    "password": "PasswordSeguro2026!",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(r_ok.status_code, 200)
        cfg = ConfiguracionEvento.obtener()
        self.assertTrue(cfg.modo_prueba_activo)
        self.assertEqual(cfg.turno_simulado, ConfiguracionEvento.TURNO_MANANA)

    def test_importar_padron_csv_xlsx_sqlite_y_exportar_excel(self):
        # CSV
        csv_bytes = "codigo_alumno,dni,apellidos_nombres\n20230001,70000001,GARCIA PEREZ ANA\n".encode("utf-8")
        res_csv = importar_padron_desde_archivo("padron.csv", csv_bytes)
        self.assertEqual(res_csv["creados"], 1)

        # SQLite .db externo
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            db_path = tmp.name
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE alumnos (codigo TEXT, dni TEXT, apellidos_nombres TEXT)")
        conn.execute("INSERT INTO alumnos VALUES ('20230002', '70000002', 'MENDOZA SILVA LUIS')")
        conn.commit()
        conn.close()

        call_command("cargar_padron", db_path)
        self.assertTrue(PadronMatriculado.objects.filter(dni="70000002").exists())

        # Exportación Excel 3 hojas
        self._login_admin()
        res_xlsx = self.client.get(reverse("exportar_excel"))
        self.assertEqual(res_xlsx.status_code, 200)
        wb = openpyxl.load_workbook(io.BytesIO(res_xlsx.content))
        self.assertEqual(len(wb.sheetnames), 3)
        self.assertEqual(
            wb.sheetnames,
            ["Resumen del Congreso", "Consolidado y Certificación", "Auditoría de Escaneos"],
        )

    def test_paginacion_dashboard_y_numeracion_participantes(self):
        # Crear 12 participantes activos para probar paginación de 10 por página
        for i in range(1, 13):
            Participante.objects.create(
                dni=f"710000{i:02d}",
                codigo_alumno=f"2024{i:04d}",
                apellidos_nombres=f"ALUMNO PRUEBA {i:02d}",
                estado_pago=Participante.ESTADO_PAGADO,
            )

        self._login_admin()
        r1 = self.client.get(reverse("api_dashboard_data") + "?page=1&page_size=10")
        self.assertEqual(r1.status_code, 200)
        d1 = r1.json()
        self.assertEqual(d1["paginacion"]["total_filtrado"], 12)
        self.assertEqual(d1["paginacion"]["num_pages"], 2)
        self.assertEqual(len(d1["participantes"]), 10)
        self.assertEqual(d1["participantes"][0]["numero"], 1)
        self.assertEqual(d1["participantes"][9]["numero"], 10)
        self.assertNotIn("total_recaudado", d1["kpis"])
        self.assertNotIn("pendientes_pago", d1["kpis"])

        r2 = self.client.get(reverse("api_dashboard_data") + "?page=2&page_size=10")
        d2 = r2.json()
        self.assertEqual(len(d2["participantes"]), 2)
        self.assertEqual(d2["participantes"][0]["numero"], 11)
        self.assertEqual(d2["participantes"][1]["numero"], 12)

        # Vista pública solo muestra consulta y descarga de QR
        r_pub = self.client.get(reverse("registro_pasos"))
        self.assertEqual(r_pub.status_code, 200)
        self.assertContains(r_pub, "Consultar y Descargar mi Pase QR")
        self.assertNotContains(r_pub, "Continuar a Verificación de Pago")

    @override_settings(GOOGLE_SHEET_CSV_URL="https://example.com/sheet.csv")
    def test_sincronizacion_solo_lectura_google_sheet(self):
        csv_mock = (
            ",,,,,,,,\n"
            ",Registro de Pagos,,,,,,,\n"
            ",N°,Nombres y Apellidos,DNI,Código de Estudiante,Monto,Método de Pago,,\n"
            ",1,Alumno Nuevo Uno,74561234,2026110001,S/ 60.00,Yape,,\n"
            ",2,Invitado Fuera,74569999,INVITADO EXTERNO,S/ 40.00,Yape,,\n"
            ",3,Sin Dni Valido,DOCENTE,DOCENTE,S/ 50.00,Efectivo,,\n"
        ).encode("utf-8")

        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return csv_mock

        with patch("acreditacion.utils.urllib.request.urlopen", return_value=_FakeResp()):
            res = sincronizar_desde_google_sheet(force=True)
            self.assertTrue(res["ok"])
            self.assertEqual(res["creados"], 1)
            self.assertEqual(res["excluidos_externos"], 1)
            self.assertEqual(res["omitidos_dni"], 1)

        p = Participante.objects.get(dni="74561234")
        self.assertEqual(p.apellidos_nombres, "ALUMNO NUEVO UNO")
        self.assertEqual(p.codigo_alumno, "2026110001")
        self.assertEqual(p.estado_pago, Participante.ESTADO_PAGADO)
        self.assertTrue(verificar_firma_token_qr(p.qr_token)[0])
        self.assertFalse(Participante.objects.filter(dni="74569999").exists())


