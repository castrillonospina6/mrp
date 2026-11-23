import os
import sys
from pathlib import Path

from mes_core.plugin_loader import discover_plugins

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Lee variables KEY=valor de un archivo .env (sin dependencias). Las del entorno real tienen prioridad."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():  # -sig: tolera el BOM que agrega PowerShell
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _csv(name: str, default: str = "") -> list[str]:
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


_load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.environ.get("MES_SECRET_KEY", "dev-insecure-change-me")
DEBUG = os.environ.get("MES_DEBUG", "1") == "1"

# Hosts permitidos: local + túneles de Cloudflare (*.trycloudflare.com cambia en cada arranque) + los del .env
# (p. ej. un dominio propio de un túnel con nombre: MES_ALLOWED_HOSTS=mes.miempresa.com).
def _own_addresses() -> list[str]:
    """Nombre e IPs de este PC en la red local: las tablets entran por http://IP-DEL-PC:8508/p/piso/."""
    import socket

    try:
        name, aliases, ips = socket.gethostbyname_ex(socket.gethostname())
        return [name, *aliases, *[ip for ip in ips if not ip.startswith(("127.", "169.254."))]]
    except OSError:
        return []


# Túneles: serveo (*.serveousercontent.com y *.serveo.net; el subdominio puede cambiar) y Cloudflare.
TUNNEL_HOSTS = [".serveousercontent.com", ".serveo.net", ".trycloudflare.com"]
ALLOWED_HOSTS = ["localhost", "127.0.0.1", *TUNNEL_HOSTS, *_own_addresses(), *_csv("MES_ALLOWED_HOSTS")]
# El túnel entrega la página por HTTPS: Django debe confiar en ese origen para aceptar formularios (CSRF).
CSRF_TRUSTED_ORIGINS = [*[f"https://*{h}" for h in TUNNEL_HOSTS],
                        *[f"https://{h.lstrip('.')}" for h in _csv("MES_ALLOWED_HOSTS") if h not in ("*",)]]

CORE_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "mes_core",
]

# Los plugins se descubren solos (carpeta plugins/ + entry points "mes.plugins").
MES_PLUGIN_APPS = discover_plugins(BASE_DIR / "plugins")
INSTALLED_APPS = CORE_APPS + MES_PLUGIN_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Comprime HTML, JSON, CSS y JS (3 a 6 veces menos datos por la Wi-Fi de planta y por el túnel)
    "django.middleware.gzip.GZipMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "mes_core.context_processors.mes",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# SQLite para desarrollo. Para producción: definir MES_DB_ENGINE=postgresql y las MES_DB_*.
if os.environ.get("MES_DB_ENGINE") == "postgresql":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("MES_DB_NAME", "mes"),
            "USER": os.environ.get("MES_DB_USER", "mes"),
            "PASSWORD": os.environ.get("MES_DB_PASSWORD", ""),
            "HOST": os.environ.get("MES_DB_HOST", "localhost"),
            "PORT": os.environ.get("MES_DB_PORT", "5432"),
        }
    }
else:
    # Varias tablets escriben a la vez. WAL deja leer mientras otro escribe; IMMEDIATE + timeout hacen que las
    # escrituras esperen su turno (milisegundos) en vez de fallar con «database is locked».
    DATABASES = {"default": {
        "ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3",
        "OPTIONS": {
            "transaction_mode": "IMMEDIATE",
            "timeout": 30,
            "init_command": ("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA busy_timeout=30000; "
                             "PRAGMA temp_store=MEMORY; PRAGMA cache_size=-32000;"),
        },
    }}

# Sesiones en memoria (con respaldo en la base): cada consulta de una tablet no lee la tabla de sesiones
SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]

LANGUAGE_CODE = "es"
TIME_ZONE = os.environ.get("MES_TIME_ZONE", "America/Bogota")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "login"

# Avisos por correo (opcional). Sin MES_EMAIL_HOST los avisos quedan solo en la bandeja de la aplicación.
MES_EMAIL_NOTIFICATIONS = bool(os.environ.get("MES_EMAIL_HOST"))
if MES_EMAIL_NOTIFICATIONS:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_HOST = os.environ["MES_EMAIL_HOST"]
    EMAIL_PORT = int(os.environ.get("MES_EMAIL_PORT", "587"))
    EMAIL_HOST_USER = os.environ.get("MES_EMAIL_USER", "")
    EMAIL_HOST_PASSWORD = os.environ.get("MES_EMAIL_PASSWORD", "")
    EMAIL_USE_TLS = os.environ.get("MES_EMAIL_TLS", "1") == "1"
    EMAIL_TIMEOUT = 10
DEFAULT_FROM_EMAIL = os.environ.get("MES_EMAIL_FROM", "MES Planta <no-responder@localhost>")
# Revisiones automáticas (asistencia, atrasos, polivalencia) al usar la app, máx. cada 5 min. Apagadas en las pruebas.
MES_AUTO_CHECKS = os.environ.get("MES_AUTO_CHECKS", "1") == "1" and sys.argv[1:2] != ["test"]
MES_BASE_URL = os.environ.get("MES_BASE_URL", "http://localhost:8508").rstrip("/")  # para los enlaces del correo
# Validación EAN: 1 = no se puede reportar producción de un pedido hasta que le carguen sus EAN (o lo marquen «No
# aplica»). 0 (por defecto) = se deja reportar sin referencia mientras tanto, y el pedido sale con el aviso.
MES_EAN_EXIGIR = os.environ.get("MES_EAN_EXIGIR", "0") == "1"
# Control de piso: hora en que se cierran solas las sesiones de todas las personas en las tablets (las terminales son
# compartidas; al otro día cada quien entra en la que le toque). Vacío = no cerrar. Las tablets fijas de línea no se cierran.
PISO_CIERRE_SESIONES = os.environ.get("PISO_CIERRE_SESIONES", "22:00")
