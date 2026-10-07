"""Idempotent local-only account provisioning; passwords are never supplied."""
import os
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from portal.role_access import BROWSE_PERMISSIONS, HOSPITAL_GROUP, INTERNAL_GROUP


class Command(BaseCommand):
    help = "Configure local Claire/Cynthia Portal roles. Dry-run by default; use --apply."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        database = settings.DATABASES["default"]
        settings_module = settings.SETTINGS_MODULE or os.environ.get("DJANGO_SETTINGS_MODULE")
        if (settings_module not in {"config.settings", "config.settings_test"}
                or ("postgresql" in database["ENGINE"] and database.get("HOST") not in {"localhost", "127.0.0.1", ""})):
            raise CommandError(f"This command only supports the local development or isolated test database (settings={settings.SETTINGS_MODULE}, engine={database['ENGINE']}).")
        User = get_user_model()
        with transaction.atomic():
            cynthia = User.objects.select_for_update().filter(username="Cynthia").first()
            claire = User.objects.select_for_update().filter(username="Claire").first()
            if cynthia is None:
                raise CommandError("Existing Cynthia account not found; no changes made.")
            if (not cynthia.is_active or not cynthia.is_staff
                    or cynthia.groups.filter(name__in=[HOSPITAL_GROUP, "Hospital Demo"]).exists()):
                raise CommandError("Cynthia has an incompatible existing account state; no changes made.")
            if claire is not None and (not claire.is_active or claire.is_staff or claire.is_superuser
                    or claire.groups.filter(name__in=[INTERNAL_GROUP, "Hospital Demo"]).exists()
                    or not claire.get_all_permissions().issubset(BROWSE_PERMISSIONS)):
                raise CommandError("Claire already exists with an incompatible identity or permissions; no changes made.")
            permissions = list(Permission.objects.filter(codename__in=[p.split(".")[1] for p in BROWSE_PERMISSIONS]).select_related("content_type"))
            permissions = [p for p in permissions if f"{p.content_type.app_label}.{p.codename}" in BROWSE_PERMISSIONS]
            if len(permissions) != len(BROWSE_PERMISSIONS):
                raise CommandError("Required existing browse permissions are missing; no changes made.")
            if not options["apply"]:
                self.stdout.write("Local dry-run: configure Claire with browse-only permissions and preserve Cynthia's existing identity and permissions.")
                return
            hospital_group, _ = Group.objects.get_or_create(name=HOSPITAL_GROUP)
            internal_group, _ = Group.objects.get_or_create(name=INTERNAL_GROUP)
            if internal_group.permissions.exists():
                raise CommandError("Internal role marker already grants permissions; no changes made.")
            existing_group_permissions = set(hospital_group.permissions.values_list("content_type__app_label", "codename"))
            if any(f"{app}.{code}" not in BROWSE_PERMISSIONS for app, code in existing_group_permissions):
                raise CommandError("Hospital role group already has elevated permissions; no changes made.")
            hospital_group.permissions.add(*permissions)
            created = claire is None
            if created:
                claire = User.objects.create_user(username="Claire", password=None, is_active=True, is_staff=False, is_superuser=False)
            claire.groups.add(hospital_group)
            cynthia.groups.add(internal_group)
            self.stdout.write(self.style.SUCCESS("Claire created with unusable password." if created else "Existing Claire preserved; role configured."))
            self.stdout.write("Cynthia retained; password, profile, flags and existing permissions unchanged.")
            if not claire.has_usable_password():
                self.stdout.write("Set Claire's password interactively: python manage.py changepassword Claire")
