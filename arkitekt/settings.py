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
from delt.initialize import initialize


initialize()

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/3.1/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = '9=9u7c35!*p_h674kv*t^8ntefnf*#)z_h%6$#b(oe=_mwysw+'

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = True

ALLOWED_HOSTS = ["*"]


ELEMENTS_HOST = "p-tnagerl-lab1"
ELEMENTS_INWARD = "arkitekt" # Set this to the host you are on
ELEMENTS_PORT = 8090 # Set this to the host you are on

# Uncomment and re run
OAUTH2_PROVIDER_APPLICATION_MODEL='oauth2_provider.Application'


# S3 Settings
S3_PUBLIC_DOMAIN = f"{ELEMENTS_HOST}:9000" #TODO: FIx
AWS_ACCESS_KEY_ID = "weak_access_key"
AWS_SECRET_ACCESS_KEY = "weak_secret_key"
AWS_S3_ENDPOINT_URL  = f"http://minio:9000"
AWS_STORAGE_BUCKET_NAME = "test"
AWS_S3_URL_PROTOCOL = "http:"
AWS_S3_FILE_OVERWRITE = False
AWS_DEFAULT_ACL = None
AWS_S3_USE_SSL = True
AWS_S3_SECURE_URLS = False # Should resort to True if using in Production behind TLS


# Application definition

DELT = {
    "INWARD": "elements",
    "OUTWARD": ELEMENTS_HOST,
    "PORT": ELEMENTS_PORT,
    "TYPE": "graphql"
}




HERRE = {
    "PUBLIC_KEY": """
-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAvIrkAA1Tr8pLRR08xXEs
zuyi/+QGRQ3J7o5j7B+HJLv2MWppd+fgoPQYc9nOkZcA9Jizsvm0bqcXe/8zdxaU
z7bA+nq3hxLolO4q4SXRxNuBIcNrfLizFrWku5csO9ZfS4EXQGOGAWsVE1WbSRBC
gAcOR8eq8gB0ai4UByB/xGlwobz1bkuXd3jGVN2oeCo7gbij/JaMrOSkh9wX/WqZ
lbrEWEFfgURENACn45Hm4ojjLepw/b2j7ZrHMQxvY1THi6lZ6bp9NdfkzyE6JhZb
SVOzd/dHy+gLBx2UuvmovVEhhxzwRJYtPdqlOWuUOjO24AlpPv7j+BSY7eGSzYU5
oQIDAQAB
-----END PUBLIC KEY-----""",
    "KEY_TYPE": "RS256",
    "ISSUER": "arnheim"
}

GRUNNLAG = {
    "GROUPS": None

}



EXTENSIONS = [
    'balder',
    'facade',
    "hare"
]




INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django_filters',
    'taggit',
    'channels',
    'herre',
    'guardian',
    'graphene_django',
    "rest_framework",
    'oauth2_provider'
] + EXTENSIONS



MIDDLEWARE = [

    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'herre.middlewares.request.jwt.JWTTokenMiddleWare',
    'herre.middlewares.request.bouncer.BouncedMiddleware', # needs to be after JWT and session 
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]


CORS_ORIGIN_ALLOW_ALL = True
ROOT_URLCONF = 'arkitekt.urls'


TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [
            'templates',
        ],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'arkitekt.wsgi.application'
ASGI_APPLICATION = 'arkitekt.routing.application'

# Database
# https://docs.djangoproject.com/en/3.1/ref/settings/#databases

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB"),
        "USER": os.environ.get("POSTGRES_USER"),
        "PASSWORD":os.environ.get("POSTGRES_PASSWORD"),
        "HOST": os.environ.get("POSTGRES_SERVICE_HOST"),
        "PORT": os.environ.get("POSTGRES_SERVICE_PORT"),
    }
}

CHANNEL_LAYERS = {
    "default": {
        # This example app uses the Redis channel layer implementation channels_redis
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [("redis",6379)],
        },
    },
}

AUTHENTICATION_BACKENDS = ('django.contrib.auth.backends.ModelBackend', 'guardian.backends.ObjectPermissionBackend')

# Password validation
# https://docs.djangoproject.com/en/3.1/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

GRAPHENE = {
    "SCHEMA": "balder.schema.graphql_schema"
}

# Internationalization
# https://docs.djangoproject.com/en/3.1/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_L10N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/3.1/howto/static-files/

STATIC_URL = '/static/'
STATICFILES_DIRS = [
    os.path.join(BASE_DIR, "static"),
]


LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'console': {
            '()': 'colorlog.ColoredFormatter',  # colored output
            # exact format is not important, this is the minimum information
            'format': '%(log_color)s[%(levelname)s]  %(name)s %(asctime)s :: %(message)s',
            'log_colors': {
                'DEBUG':    'bold_black',
                'INFO':     'green',
                'WARNING':  'yellow',
                'ERROR':    'red',
                'CRITICAL': 'bold_red',
            },
        },
    },
    'handlers': {
        'console': {
            'class': 'colorlog.StreamHandler',
            'formatter': 'console',
        },
    },
    'loggers': {
    # root logger
        '': {
            'level': "INFO",
            'handlers': ['console'],
        },
        'oauthlib': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': True,
        },
        'delt': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
        'oauth2_provider': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
    },
}