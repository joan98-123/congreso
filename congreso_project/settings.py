"""
Configuración principal del proyecto Congreso Universitario.
Incluye configuración regional America/Lima, SQLite optimizado con timeout/WAL
y cabeceras de seguridad OWASP.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# En producción definir DJANGO_SECRET_KEY en variables de entorno
SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-congreso-univ-dev-key-cambiar-en-produccion-9f4c2a1e8b7d",
)

IS_VERCEL = bool(os.environ.get("VERCEL"))

DEBUG = os.environ.get("DJANGO_DEBUG", "False" if IS_VERCEL else "True").lower() in ("1", "true", "yes")

ALLOWED_HOSTS = (
    [h.strip() for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "").split(",") if h.strip()]
    if os.environ.get("DJANGO_ALLOWED_HOSTS")
    else ([".vercel.app", "localhost", "127.0.0.1"] if IS_VERCEL else ["*"] if DEBUG else ["localhost", "127.0.0.1"])
)

# Permitir orígenes locales / LAN / Vercel para formularios CSRF
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]
if IS_VERCEL:
    CSRF_TRUSTED_ORIGINS.append("https://*.vercel.app")
    for env_host in ("VERCEL_URL", "VERCEL_PROJECT_PRODUCTION_URL"):
        host = os.environ.get(env_host, "").strip()
        if host:
            CSRF_TRUSTED_ORIGINS.append(f"https://{host}")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "acreditacion.apps.AcreditacionConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "congreso_project.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "congreso_project.wsgi.application"
ASGI_APPLICATION = "congreso_project.asgi.application"

# Base de datos SQLite con timeout ampliado; en Vercel Serverless el sistema de archivos
# es de solo lectura excepto /tmp, por lo que se usa /tmp/db.sqlite3 salvo que se configure
# una base de datos externa. El modo WAL se activa en acreditacion/apps.py.
SQLITE_PATH = Path("/tmp/db.sqlite3") if IS_VERCEL else (BASE_DIR / "db.sqlite3")

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": SQLITE_PATH,
        "OPTIONS": {
            "timeout": 20,
        },
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "es-pe"
TIME_ZONE = "America/Lima"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Rutas de autenticación
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/panel/"
LOGOUT_REDIRECT_URL = "/login/"

# Seguridad de sesiones y cabeceras HTTP (OWASP)
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_EXPIRE_AT_BROWSER_CLOSE = False
SESSION_COOKIE_AGE = 60 * 60 * 12  # 12 horas (duración de la jornada del congreso)
CSRF_COOKIE_SAMESITE = "Lax"
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"

if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_BROWSER_XSS_FILTER = True

# Reglas de negocio del Congreso Universitario (centralizadas en backend)
CONGRESO_CONFIG = {
    "NOMBRE_EVENTO": "Congreso CIMA 2026",
    "SUBTITULO_EVENTO": "Acreditación Oficial y Control de Asistencia",
    "TARIFA_MATRICULADO": "60.00",
    "TARIFA_EXTERNO": "40.00",
    "MANANA_INICIO": (9, 0),   # 09:00 AM
    "MANANA_FIN": (13, 0),     # 01:00 PM
    "TARDE_INICIO": (15, 0),   # 03:00 PM
    "TARDE_FIN": (19, 0),      # 07:00 PM
    "YAPE_NUMERO": "999 888 777",
    "YAPE_TITULAR": "Tesorería Congreso Universitario",
    "PLIN_NUMERO": "999 888 777",
}

# Fuente de solo lectura (Google Sheets CSV export). El sistema NUNCA modifica la hoja.
GOOGLE_SHEET_CSV_URL = os.environ.get(
    "GOOGLE_SHEET_CSV_URL",
    "https://docs.google.com/spreadsheets/d/1IDU8gAJ7wUm3GbPN9b_IHnS3qiM3dDWYREGWMFiQR4Y/export?format=csv&gid=0",
)
GOOGLE_SHEET_SYNC_INTERVAL = int(os.environ.get("GOOGLE_SHEET_SYNC_INTERVAL", "30"))
