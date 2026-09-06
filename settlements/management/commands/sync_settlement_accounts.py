from django.core.management.base import (
    BaseCommand,
    CommandError,
)

from documents.models import GeneratedDocument
from settlements.services.settlement_sync_service import (
    sync_all_settlement_accounts,
    sync_document_to_settlement,
)


class Command(BaseCommand):
    help = (
        "从已有 Hospital Invoice 和 Factory PO "
        "创建结算账户。"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help=(
                "仅校验和预览，不写入数据库。"
            ),
        )

        parser.add_argument(
            "--document-id",
            type=int,
            default=None,
            help=(
                "只同步指定 GeneratedDocument。"
            ),
        )

        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help=(
                "最多扫描指定数量的文档。"
            ),
        )

    def handle(
        self,
        *args,
        **options,
    ):
        dry_run = options["dry_run"]
        document_id = options[
            "document_id"
        ]
        limit = options["limit"]

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    "DRY RUN：不会写入数据库。"
                )
            )

        if document_id is not None:
            try:
                document = (
                    GeneratedDocument.objects
                    .get(pk=document_id)
                )
            except (
                GeneratedDocument.DoesNotExist
            ) as exc:
                raise CommandError(
                    f"GeneratedDocument "
                    f"{document_id} 不存在。"
                ) from exc

            result = (
                sync_document_to_settlement(
                    document,
                    dry_run=dry_run,
                )
            )

            self._print_result(result)

            if result["status"] == "error":
                raise CommandError(
                    result["message"]
                )

            return

        report = (
            sync_all_settlement_accounts(
                dry_run=dry_run,
                limit=limit,
            )
        )

        for result in report["results"]:
            self._print_result(result)

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                "同步结果："
            )
        )

        self.stdout.write(
            f"扫描：{report['scanned']}"
        )
        self.stdout.write(
            f"新建：{report['created']}"
        )
        self.stdout.write(
            "可创建："
            f"{report['would_create']}"
        )
        self.stdout.write(
            f"已存在：{report['existing']}"
        )
        self.stdout.write(
            f"错误：{report['error']}"
        )

        if report["error"]:
            self.stdout.write(
                self.style.WARNING(
                    "存在无法同步的文档，"
                    "请检查上面的错误信息。"
                )
            )

    def _print_result(
        self,
        result,
    ):
        status = result["status"]

        prefix_map = {
            "created": "CREATED",
            "would_create": "READY",
            "existing": "EXISTS",
            "unsupported": "SKIP",
            "error": "ERROR",
        }

        text = (
            f"[{prefix_map.get(status, status)}] "
            f"ID={result['document_id']} "
            f"{result['document_number']} — "
            f"{result['message']}"
        )

        if status in {
            "created",
            "would_create",
        }:
            account = result.get("account")

            if account:
                text += (
                    f" | "
                    f"{account.get_direction_display()}"
                    f" | "
                    f"{account.original_amount} "
                    f"{account.currency}"
                    f" | "
                    f"{account.counterparty_name}"
                )

        if status == "error":
            self.stdout.write(
                self.style.ERROR(text)
            )

        elif status == "existing":
            self.stdout.write(
                self.style.WARNING(text)
            )

        else:
            self.stdout.write(text)
