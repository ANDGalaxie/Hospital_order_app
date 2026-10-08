from django.core.management.base import BaseCommand
from administrative_expenses.services import recover_files


class Command(BaseCommand):
    help = "Recover interrupted administrative uploads and retry private attachment deletions."

    def handle(self, *args, **options):
        self.stdout.write(f"Recovered files: {recover_files()}")
