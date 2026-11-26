import os
from django.core.management.base import BaseCommand, CommandError
from acreditacion.utils import importar_padron_desde_archivo


class Command(BaseCommand):
    help = (
        "Importa masivamente el padrón de alumnos matriculados desde un archivo "
        ".csv, .xlsx o .db/.sqlite3."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "archivo",
            type=str,
            help="Ruta al archivo de padrón (.csv, .xlsx, .db o .sqlite3).",
        )

    def handle(self, *args, **options):
        ruta = options["archivo"]
        if not os.path.isfile(ruta):
            raise CommandError(f"No se encontró el archivo: {ruta}")

        with open(ruta, "rb") as f:
            contenido = f.read()

        try:
            resumen = importar_padron_desde_archivo(os.path.basename(ruta), contenido)
        except Exception as exc:
            raise CommandError(f"Error al importar padrón: {exc}") from exc

        self.stdout.write(
            self.style.SUCCESS(
                f"Importación completada: {resumen['creados']} creados, "
                f"{resumen['actualizados']} actualizados, {resumen['omitidos']} omitidos "
                f"(de {resumen['total_leidos']} filas leídas)."
            )
        )
        for err in resumen["errores"]:
            self.stdout.write(self.style.WARNING(f"  - {err}"))
