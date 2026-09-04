"""
Database router — all webserving models go to commerce_webserving.

WebServing has its own database. No FK into WebClerk tables.
No pending records, no denormalize stack, no BaseModel machinery.
"""

APP = 'webserving'
DB = 'webserving'


class WebServingRouter:
    """Route apps.webserving models to the webserving database."""

    def db_for_read(self, model, **hints):
        if model._meta.app_label == APP:
            return DB
        return None

    def db_for_write(self, model, **hints):
        if model._meta.app_label == APP:
            return DB
        return None

    def allow_relation(self, obj1, obj2, **hints):
        if obj1._meta.app_label == APP or obj2._meta.app_label == APP:
            return obj1._meta.app_label == obj2._meta.app_label
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if app_label == APP:
            return db == DB
        if db == DB:
            return False
        return None
