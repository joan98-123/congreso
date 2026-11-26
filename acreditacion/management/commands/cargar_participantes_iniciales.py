import json
import uuid
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from acreditacion.models import Participante
from acreditacion.utils import firmar_uuid_qr


class Command(BaseCommand):
    help = "Carga idempotentemente la lista oficial de participantes con sus pases QR activos."

    def handle(self, *args, **options):
        data_file = Path(settings.BASE_DIR) / "acreditacion" / "data" / "participantes_iniciales.json"
        if not data_file.exists():
            self.stdout.write(self.style.WARNING(f"No se encontró {data_file}"))
            return

        registros = json.loads(data_file.read_text(encoding="utf-8"))
        creados = 0
        existentes = 0
        ahora = timezone.now()

        with transaction.atomic():
            for item in registros:
                dni = item["dni"].strip()
                if Participante.objects.filter(dni=dni).exclude(estado_pago=Participante.ESTADO_ANULADO).exists():
                    existentes += 1
                    continue

                uid = uuid.UUID(item["qr_uuid"])
                token = firmar_uuid_qr(uid)

                Participante.objects.create(
                    dni=dni,
                    codigo_alumno=(item.get("codigo_alumno") or "").strip().upper(),
                    apellidos_nombres=item["apellidos_nombres"].strip().upper(),
                    es_matriculado=True,
                    tipo_tarifa=Participante.TARIFA_MATRICULADO,
                    monto_pagar=Decimal("0.00"),
                    estado_pago=Participante.ESTADO_PAGADO,
                    fecha_validacion_pago=ahora,
                    qr_uuid=uid,
                    qr_token=token,
                )
                creados += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Participantes iniciales cargados: {creados} nuevos, {existentes} ya existentes (total: {len(registros)})."
            )
        )
