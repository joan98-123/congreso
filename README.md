# Sistema de Acreditación y Control de Doble Asistencia — Congreso Universitario

Sistema web completo desarrollado con **Python 3.12, Django 5.2 y SQLite (modo WAL)** para el registro en puerta, validación de pagos, generación de pases QR firmados criptográficamente (`UUIDv4` + `HMAC-SHA256`) y control de doble asistencia por franjas horarias (`09:00–13:00` y `15:00–19:00`, zona horaria `America/Lima`).

## Instalación y Uso Local

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser
python manage.py cargar_padron ruta\al\padron.xlsx
python manage.py runserver 0.0.0.0:8000
```

## Rutas Principales
- `/` — Inscripción en puerta (Stepper de 4 pasos) + Recuperación de Pase QR por DNI
- `/login/` — Acceso de Staff / Administradores
- `/panel/` — Dashboard de control en tiempo real, cola de pagos, carga de padrón y exportación Excel (`.xlsx`)
- `/escaner/` — Escáner QR de doble asistencia con cámara web (`html5-qrcode`)

## Despliegue en Vercel
El proyecto incluye `vercel.json` y soporte para Vercel Python Runtime (`congreso_project/wsgi.py`).
> **Nota:** En Vercel Serverless, SQLite en `/tmp/db.sqlite3` es efímero entre arranques en frío. Para persistencia garantizada en producción durante el evento, se recomienda conectar una base de datos PostgreSQL (como Neon Postgres desde Vercel Marketplace) o ejecutar localmente con SQLite en modo WAL.
