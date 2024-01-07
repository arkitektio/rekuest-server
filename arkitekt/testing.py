"""
Django settings for elements project.

Generated by 'django-admin startproject' using Django 3.1.7.

For more information on this file, see
https://docs.djangoproject.com/en/3.1/topics/settings/

For the full list of settings and their values, see
https://docs.djangoproject.com/en/3.1/ref/settings/
"""
import os
from pathlib import Path
from omegaconf import OmegaConf

conf = OmegaConf.load("config.yaml")

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/3.1/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = conf.server.secret_key

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = conf.server.debug
ALLOWED_HOSTS = conf.server.hosts
CORS_ORIGIN_ALLOW_ALL = True

LOK = {
    "PUBLIC_KEY": conf.lok.public_key,
    "KEY_TYPE": conf.lok.key_type,
    "ISSUER": conf.lok.issuer,
}

SUPERUSERS = [
    {
        "USERNAME": conf.server.admin.username,
        "EMAIL": conf.server.admin.email,
        "PASSWORD": conf.server.admin.password,
    }
]


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_filters",
    "corsheaders",
    "taggit",
    "channels",
    "health_check",
    "health_check.db",
    "health_check.contrib.rabbitmq",  # requires RabbitMQ broker
    "health_check.contrib.redis",
    "lok",
    "django_probes",
    "guardian",
    "graphene_django",
    "rest_framework",
    "balder",
    "facade",
    "hare",
]

HEALTH_CHECK = {
    "DISK_USAGE_MAX": 90,  # percent
    "MEMORY_MIN": 100,  # in MB
}


IMITATE_GROUPS = ["team:sibarita"]


MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "lok.middlewares.request.jwt.JWTTokenMiddleWare",
    "lok.middlewares.request.bouncer.BouncedMiddleware",  # needs to be after JWT and session
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]


ROOT_URLCONF = "arkitekt.urls"


TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [
            "templates",
        ],
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

WSGI_APPLICATION = "arkitekt.wsgi.application"
ASGI_APPLICATION = "arkitekt.asgi.application"

# Database
# https://docs.djangoproject.com/en/3.1/ref/settings/#databases

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": "mydatabase",
    }
}


REDIS_URL = f"redis://{conf.redis.host}:{conf.redis.port}"
BROKER_URL = f"amqp://{conf.rabbit.username}:{conf.rabbit.password}@{conf.rabbit.host}:{conf.rabbit.port}/{conf.rabbit.v_host}"


DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

CHANNEL_LAYERS = {
    "default": {
        # This example app uses the Redis channel layer implementation channels_redis
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [(conf.redis.host, conf.redis.port)],
            "prefix": "rekuest"
        },
    },
}

AUTH_USER_MODEL = "lok.LokUser"

AUTHENTICATION_BACKENDS = (
    "django.contrib.auth.backends.ModelBackend",
    "guardian.backends.ObjectPermissionBackend",
)

# Password validation
# https://docs.djangoproject.com/en/3.1/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

GRAPHENE = {"SCHEMA": "balder.schema.graphql_schema"}

# Internationalization
# https://docs.djangoproject.com/en/3.1/topics/i18n/

LANGUAGE_CODE = "en-us"

TIME_ZONE = "UTC"

USE_I18N = True

USE_L10N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/3.1/howto/static-files/

STATIC_URL = "static/"
STATICFILES_DIRS = [
    os.path.join(BASE_DIR, "static"),
]

STATIC_ROOT = "/var/www/static/"

STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "console": {
            # exact format is not important, this is the minimum information
            "format": "%(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "rich.logging.RichHandler",
            "formatter": "console",
            "rich_tracebacks": True,
        },
    },
    "loggers": {
        # root logger
        "": {
            "level": "INFO",
            "handlers": ["console"],
        },
        "oauthlib": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": True,
        },
        "delt": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "oauth2_provider": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
    },
}

MEDIA_URL = "media/"
MEDIA_ROOT = "/media"
