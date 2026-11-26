import re
from django import forms
from .models import Participante


class RegistroParticipanteForm(forms.Form):
    """
    Formulario estricto para el Paso 1 de inscripción en puerta.
    No solicita número de celular. El monto se calcula siempre en backend.
    """

    dni = forms.CharField(
        max_length=8,
        min_length=8,
        label="DNI",
        widget=forms.TextInput(
            attrs={
                "inputmode": "numeric",
                "pattern": r"\d{8}",
                "maxlength": "8",
                "placeholder": "Ej. 71234567",
                "autocomplete": "off",
            }
        ),
    )
    codigo_alumno = forms.CharField(
        max_length=32,
        required=False,
        label="Código de Alumno (Opcional si es externo)",
        widget=forms.TextInput(
            attrs={
                "maxlength": "32",
                "placeholder": "Ej. 20210458 (dejar vacío si es externo)",
                "autocomplete": "off",
            }
        ),
    )
    apellidos_nombres = forms.CharField(
        max_length=200,
        min_length=5,
        label="Apellidos y Nombres",
        widget=forms.TextInput(
            attrs={
                "maxlength": "200",
                "placeholder": "Ej. Pérez García, Juan Carlos",
                "autocomplete": "name",
            }
        ),
    )

    def clean_dni(self):
        dni = (self.cleaned_data.get("dni") or "").strip()
        if not re.fullmatch(r"\d{8}", dni):
            raise forms.ValidationError("El DNI debe contener exactamente 8 dígitos numéricos.")
        if Participante.objects.filter(dni=dni).exclude(estado_pago=Participante.ESTADO_ANULADO).exists():
            raise forms.ValidationError(
                "Este DNI ya cuenta con una inscripción activa. Usa la opción 'Recuperar mi Pase QR' o consulta en caja."
            )
        return dni

    def clean_codigo_alumno(self):
        codigo = (self.cleaned_data.get("codigo_alumno") or "").strip().upper()
        if codigo and not re.fullmatch(r"[A-Z0-9\-_]{3,32}", codigo):
            raise forms.ValidationError(
                "El código de alumno solo puede contener letras, números o guiones (3 a 32 caracteres)."
            )
        return codigo

    def clean_apellidos_nombres(self):
        nombre = re.sub(r"\s+", " ", (self.cleaned_data.get("apellidos_nombres") or "").strip())
        if len(nombre) < 5:
            raise forms.ValidationError("Ingrese sus apellidos y nombres completos.")
        if not re.fullmatch(r"[A-Za-zÁÉÍÓÚáéíóúÑñÜü,.'\-\s]{5,200}", nombre):
            raise forms.ValidationError("El nombre contiene caracteres no permitidos.")
        return nombre.upper()


class ImportarPadronForm(forms.Form):
    archivo = forms.FileField(label="Archivo de Padrón (.csv, .xlsx o .db)")

    def clean_archivo(self):
        archivo = self.cleaned_data["archivo"]
        max_mb = 15
        if archivo.size > max_mb * 1024 * 1024:
            raise forms.ValidationError(f"El archivo excede el tamaño máximo permitido ({max_mb} MB).")
        nombre = archivo.name.lower()
        if not nombre.endswith((".csv", ".xlsx", ".xlsm", ".db", ".sqlite", ".sqlite3")):
            raise forms.ValidationError("Formato inválido. Suba un archivo .csv, .xlsx o .db/.sqlite3.")
        return archivo
