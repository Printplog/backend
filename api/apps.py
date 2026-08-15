from django.apps import AppConfig


class ApiConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'api'

    def ready(self):
        import api.signals
        import api.schema  # register OpenAPI authentication extensions
        import os
        from django.conf import settings
        
        # Configure a writable home for rembg models (U2NET_HOME)
        # We put it in media/models/.u2net so it's persistent and writable
        models_dir = os.path.join(settings.BASE_DIR, 'media', 'models')
        os.makedirs(models_dir, exist_ok=True)
        os.environ['U2NET_HOME'] = os.path.join(models_dir, '.u2net')
        
        
        # NOTE: Automatic model download disabled as per optimization plan.
        # Models should be pre-provisioned in media/models/.u2net
        pass
