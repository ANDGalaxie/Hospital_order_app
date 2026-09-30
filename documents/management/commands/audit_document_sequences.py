import json

from django.core.management.base import BaseCommand
from documents.services.document_numbering_service import audit_document_sequences


class Command(BaseCommand):
    help = "Read-only structural audit of DocumentSequence Invoice/PO base numbers."

    def handle(self, *args, **options):
        records = list(audit_document_sequences())
        self.stdout.write(json.dumps({
            "inconsistent_count": len(records),
            "records": records,
        }, ensure_ascii=False, indent=2))
