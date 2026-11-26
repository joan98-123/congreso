"""
Middleware de cabeceras de seguridad OWASP (CSP, Permissions-Policy y no-store en rutas sensibles).
"""


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        # Política estricta de permisos de navegador (cámara solo en mismo origen para escáner QR)
        response.setdefault(
            "Permissions-Policy",
            "camera=(self), microphone=(), geolocation=(), payment=()",
        )

        # Content-Security-Policy adaptada a las fuentes legítimas del proyecto
        response.setdefault(
            "Content-Security-Policy",
            (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://unpkg.com; "
                "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                "font-src 'self' https://fonts.gstatic.com data:; "
                "img-src 'self' data: blob:; "
                "connect-src 'self'; "
                "frame-ancestors 'none'; "
                "base-uri 'self'; "
                "form-action 'self';"
            ),
        )

        # Evitar caché en navegadores/proxies compartidos para rutas administrativas y APIs
        path = request.path or ""
        if path.startswith(("/panel/", "/escaner/", "/api/", "/login/")):
            response["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
            response["Pragma"] = "no-cache"

        return response
