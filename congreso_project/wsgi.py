import os
from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "congreso_project.settings")
application = get_wsgi_application()

# Inicialización automática en Vercel Serverless cuando se usa SQLite efímero en /tmp
if os.environ.get("VERCEL"):
    from django.conf import settings
    from django.core.management import call_command

    db_path = settings.DATABASES["default"]["NAME"]
    if not os.path.exists(db_path):
        call_command("migrate", interactive=False, verbosity=0)
        from django.contrib.auth import get_user_model

        User = get_user_model()
        admin_user = os.environ.get("DJANGO_ADMIN_USER", "admin")
        admin_pass = os.environ.get("DJANGO_ADMIN_PASSWORD", "Congreso2026!")
        admin_email = os.environ.get("DJANGO_ADMIN_EMAIL", "admin@congreso.edu.pe")
        if not User.objects.filter(username=admin_user).exists():
            User.objects.create_superuser(
                username=admin_user,
                email=admin_email,
                password=admin_pass,
            )

# Vercel Python Runtime busca el objeto WSGI/ASGI llamado `app`
app = application
