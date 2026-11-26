from django.apps import AppConfig
from django.db.backends.signals import connection_created


def configurar_sqlite_wal(sender, connection, **kwargs):
    """
    Activa el modo WAL (Write-Ahead Logging) y ajustes de concurrencia
    en cada nueva conexión SQLite para soportar escaneos simultáneos en puerta.
    """
    if connection.vendor == "sqlite":
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA synchronous=NORMAL;")
            cursor.execute("PRAGMA busy_timeout=10000;")
            cursor.execute("PRAGMA foreign_keys=ON;")


class AcreditacionConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "acreditacion"
    verbose_name = "Acreditación y Control de Asistencia"

    def ready(self):
        connection_created.connect(configurar_sqlite_wal)
