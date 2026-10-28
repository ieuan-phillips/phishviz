import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_root_env() -> None:
    env_path = BASE_DIR.parent / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")

        os.environ.setdefault(key, value)


_load_root_env()

PROJECT_ROOT = BASE_DIR.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "").strip()

if len(SECRET_KEY) < 32:
    raise RuntimeError(
        "DJANGO_SECRET_KEY is missing or too short. "
        "Set a strong secret in your local .env file."
    )

DEBUG = os.environ.get("PHISHVIZ_DEBUG", "False").lower() == "true"

ALLOWED_HOSTS = ["127.0.0.1", "localhost"]

azure_hostname = os.environ.get("WEBSITE_HOSTNAME")
if azure_hostname:
    ALLOWED_HOSTS.append(azure_hostname)

CSRF_TRUSTED_ORIGINS = []
if azure_hostname:
    CSRF_TRUSTED_ORIGINS.append(f"https://{azure_hostname}")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "runs.apps.RunsConfig",
    "dashboard",
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

ROOT_URLCONF = "phishviz_web.urls"

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
            ],
        },
    },
]

WSGI_APPLICATION = "phishviz_web.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": PROJECT_ROOT / "workspace" / "db.sqlite3",
    }
}

LANGUAGE_CODE = "en-gb"
TIME_ZONE = "Europe/London"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
