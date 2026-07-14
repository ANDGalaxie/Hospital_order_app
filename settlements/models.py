from decimal import Decimal

from django.conf import settings
from django.core.exceptions import (
    ValidationError,
)
from django.db import models, transaction
from django.db.models import Sum
from django.utils import timezone

from documents.models import (
    GeneratedDocument,
)


ZERO_MONEY = Decimal("0.00")
MONEY_QUANT = Decimal("0.01")


class SettlementAccount(models.Model):
    """
    正式文档对应的结算账户。

    Hospital Invoice:
        direction = receivable

    Factory Purchase Order:
        direction = payable

    Factory Order Request:
        不创建结算账户
    """

    class Direction(models.TextChoices):
        RECEIVABLE = (
            "receivable",
            "医院应收",
        )

        PAYABLE = (
            "payable",
            "工厂应付",
        )

    class Status(models.TextChoices):
        UNPAID = (
            "unpaid",
            "未付款",
        )

        PARTIALLY_PAID = (
            "partially_paid",
            "部分付款",
        )

        PAID = (
            "paid",
            "已结清",
        )

        OVERDUE = (
            "overdue",
            "已逾期",
        )

        CANCELLED = (
            "cancelled",
            "已取消",
        )

    document = models.OneToOneField(
        GeneratedDocument,
        on_delete=models.PROTECT,
        related_name="settlement_account",
        verbose_name="正式文档",
    )

    direction = models.CharField(
        max_length=20,
        choices=Direction.choices,
        editable=False,
        db_index=True,
        verbose_name="结算方向",
    )

    counterparty_name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name="医院或工厂名称快照",
    )

    issue_date = models.DateField(
        db_index=True,
        verbose_name="开立日期",
    )

    due_date = models.DateField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name="付款截止日期",
    )

    currency = models.CharField(
        max_length=3,
        default="EUR",
        verbose_name="币种",
    )

    original_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        verbose_name="应收或应付金额",
    )

    status = models.CharField(
        max_length=30,
        choices=Status.choices,
        default=Status.UNPAID,
        db_index=True,
        verbose_name="结算状态",
    )

    notes = models.TextField(
        blank=True,
        default="",
        verbose_name="结算备注",
    )

    cancellation_reason = models.TextField(
        blank=True,
        default="",
        verbose_name="取消原因",
    )

    cancelled_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="取消时间",
    )

    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name=(
            "cancelled_settlement_accounts"
        ),
        verbose_name="取消操作人",
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        ordering = [
            "-issue_date",
            "-id",
        ]

        verbose_name = "结算账户"
        verbose_name_plural = "结算账户"

        constraints = [
            models.CheckConstraint(
                condition=models.Q(
                    original_amount__gt=0
                ),
                name=(
                    "settlement_original_"
                    "amount_gt_zero"
                ),
            ),
        ]

    def __str__(self):
        return (
            f"{self.document.document_number}"
            f" / "
            f"{self.get_direction_display()}"
        )

    @staticmethod
    def expected_direction_for_document(
        document,
    ):
        if (
            document.document_type
            == GeneratedDocument
            .DocumentType
            .HOSPITAL_INVOICE
        ):
            return (
                SettlementAccount
                .Direction
                .RECEIVABLE
            )

        if (
            document.document_type
            == GeneratedDocument
            .DocumentType
            .FACTORY_PO
        ):
            return (
                SettlementAccount
                .Direction
                .PAYABLE
            )

        return None

    def clean(self):
        super().clean()

        if not self.document_id:
            return

        expected_direction = (
            self.expected_direction_for_document(
                self.document
            )
        )

        if expected_direction is None:
            raise ValidationError(
                {
                    "document": (
                        "只有 Hospital Invoice "
                        "和 Factory PO 可以创建"
                        "结算账户。"
                    ),
                }
            )

        self.direction = expected_direction

        if (
            self.original_amount is None
            or self.original_amount <= 0
        ):
            raise ValidationError(
                {
                    "original_amount": (
                        "结算金额必须大于 0。"
                    ),
                }
            )

        if (
            self.due_date
            and self.issue_date
            and self.due_date
            < self.issue_date
        ):
            raise ValidationError(
                {
                    "due_date": (
                        "付款截止日期不能早于"
                        "开立日期。"
                    ),
                }
            )

        if (
            self.status
            == self.Status.CANCELLED
            and not self.cancellation_reason
            .strip()
        ):
            raise ValidationError(
                {
                    "cancellation_reason": (
                        "取消结算账户时必须填写"
                        "取消原因。"
                    ),
                }
            )

    @property
    def posted_amount(self):
        """
        有效收付款流水的累计金额。
        """
        if not self.pk:
            return ZERO_MONEY

        total = (
            self.transactions.filter(
                status=(
                    PaymentTransaction
                    .Status
                    .POSTED
                )
            )
            .aggregate(
                total=Sum("amount")
            )
            .get("total")
            or ZERO_MONEY
        )

        return Decimal(total).quantize(
            MONEY_QUANT
        )

    @property
    def remaining_amount(self):
        remaining = (
            Decimal(self.original_amount)
            - self.posted_amount
        )

        return max(
            remaining,
            ZERO_MONEY,
        ).quantize(
            MONEY_QUANT
        )

    def calculate_status(
        self,
        as_of=None,
    ):
        """
        根据有效流水和到期日计算状态。
        """
        if (
            self.status
            == self.Status.CANCELLED
        ):
            return self.Status.CANCELLED

        as_of = (
            as_of
            or timezone.localdate()
        )

        paid = self.posted_amount

        if paid >= self.original_amount:
            return self.Status.PAID

        if (
            self.due_date
            and self.due_date < as_of
        ):
            return self.Status.OVERDUE

        if paid > ZERO_MONEY:
            return (
                self.Status.PARTIALLY_PAID
            )

        return self.Status.UNPAID

    @property
    def effective_status(self):
        """
        实时状态，避免日期跨天后数据库状态滞后。
        """
        return self.calculate_status()

    def refresh_status(self):
        if not self.pk:
            return

        new_status = self.calculate_status()

        if new_status == self.status:
            return

        type(self).objects.filter(
            pk=self.pk
        ).update(
            status=new_status,
            updated_at=timezone.now(),
        )

        self.status = new_status

    def cancel(
        self,
        *,
        user,
        reason,
    ):
        reason = str(reason or "").strip()

        if not reason:
            raise ValidationError(
                "取消结算账户必须填写原因。"
            )

        if self.posted_amount > ZERO_MONEY:
            raise ValidationError(
                "已有有效收付款流水的账户"
                "不能直接取消。"
            )

        self.status = self.Status.CANCELLED
        self.cancellation_reason = reason
        self.cancelled_at = timezone.now()
        self.cancelled_by = user

        self.save(
            update_fields=[
                "status",
                "cancellation_reason",
                "cancelled_at",
                "cancelled_by",
                "updated_at",
            ]
        )

    def save(self, *args, **kwargs):
        if self.document_id:
            expected_direction = (
                self.expected_direction_for_document(
                    self.document
                )
            )

            if expected_direction:
                self.direction = (
                    expected_direction
                )

        self.currency = (
            self.currency
            or "EUR"
        ).upper()

        self.full_clean()

        result = super().save(
            *args,
            **kwargs,
        )

        self.refresh_status()

        return result

    def delete(self, *args, **kwargs):
        raise ValidationError(
            "结算账户不能直接删除。"
            "需要保留正式财务记录。"
        )


class PaymentTransaction(models.Model):
    """
    实际收付款流水。

    direction 不单独保存：
    - receivable 账户上的流水是收款
    - payable 账户上的流水是付款

    错误流水不删除，而是冲销。
    """

    class Status(models.TextChoices):
        POSTED = (
            "posted",
            "有效",
        )

        REVERSED = (
            "reversed",
            "已冲销",
        )

    class Method(models.TextChoices):
        BANK_TRANSFER = (
            "bank_transfer",
            "银行转账",
        )

        CHEQUE = (
            "cheque",
            "支票",
        )

        CARD = (
            "card",
            "银行卡",
        )

        CASH = (
            "cash",
            "现金",
        )

        OTHER = (
            "other",
            "其他",
        )

    account = models.ForeignKey(
        SettlementAccount,
        on_delete=models.PROTECT,
        related_name="transactions",
        verbose_name="结算账户",
    )

    payment_date = models.DateField(
        default=timezone.localdate,
        db_index=True,
        verbose_name="收付款日期",
    )

    amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        verbose_name="收付款金额",
    )

    method = models.CharField(
        max_length=30,
        choices=Method.choices,
        default=Method.BANK_TRANSFER,
        verbose_name="付款方式",
    )

    reference = models.CharField(
        max_length=200,
        blank=True,
        default="",
        db_index=True,
        verbose_name="银行参考号",
    )

    notes = models.TextField(
        blank=True,
        default="",
        verbose_name="备注",
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.POSTED,
        db_index=True,
        verbose_name="流水状态",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name=(
            "created_payment_transactions"
        ),
        verbose_name="登记人",
    )

    reversed_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="冲销时间",
    )

    reversed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name=(
            "reversed_payment_transactions"
        ),
        verbose_name="冲销人",
    )

    reversal_reason = models.TextField(
        blank=True,
        default="",
        verbose_name="冲销原因",
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        ordering = [
            "-payment_date",
            "-id",
        ]

        verbose_name = "收付款流水"
        verbose_name_plural = "收付款流水"

        constraints = [
            models.CheckConstraint(
                condition=models.Q(
                    amount__gt=0
                ),
                name=(
                    "payment_transaction_"
                    "amount_gt_zero"
                ),
            ),
        ]

    def __str__(self):
        return (
            f"{self.account.document.document_number}"
            f" / {self.amount} "
            f"{self.account.currency}"
        )

    @property
    def direction(self):
        return self.account.direction

    def clean(self):
        super().clean()

        if (
            self.amount is None
            or self.amount <= 0
        ):
            raise ValidationError(
                {
                    "amount": (
                        "收付款金额必须大于 0。"
                    ),
                }
            )

        if not self.account_id:
            return

        if (
            self.account.status
            == SettlementAccount
            .Status
            .CANCELLED
        ):
            raise ValidationError(
                {
                    "account": (
                        "已取消的结算账户不能"
                        "登记收付款。"
                    ),
                }
            )

        if (
            self.status
            == self.Status.REVERSED
        ):
            if not self.reversal_reason.strip():
                raise ValidationError(
                    {
                        "reversal_reason": (
                            "冲销流水必须填写原因。"
                        ),
                    }
                )

            return

        other_transactions = (
            self.account.transactions.filter(
                status=self.Status.POSTED
            )
        )

        if self.pk:
            other_transactions = (
                other_transactions.exclude(
                    pk=self.pk
                )
            )

        already_posted = (
            other_transactions.aggregate(
                total=Sum("amount")
            ).get("total")
            or ZERO_MONEY
        )

        if (
            already_posted + self.amount
            > self.account.original_amount
        ):
            raise ValidationError(
                {
                    "amount": (
                        "本次金额将导致累计付款"
                        "超过应收或应付总额。"
                    ),
                }
            )

    def save(self, *args, **kwargs):
        if not self.account_id:
            self.full_clean()

        with transaction.atomic():
            locked_account = (
                SettlementAccount.objects
                .select_for_update()
                .get(pk=self.account_id)
            )

            if self.pk:
                original = (
                    type(self).objects
                    .select_for_update()
                    .get(pk=self.pk)
                )

                if (
                    original.account_id
                    != self.account_id
                ):
                    raise ValidationError(
                        "收付款流水创建后不能"
                        "更换结算账户。"
                    )

                if (
                    original.status
                    == self.Status.REVERSED
                    and self.status
                    != self.Status.REVERSED
                ):
                    raise ValidationError(
                        "已冲销流水不能恢复为"
                        "有效状态。"
                    )

            self.account = locked_account

            self.full_clean()

            result = super().save(
                *args,
                **kwargs,
            )

            locked_account.refresh_status()

            return result

    def reverse(
        self,
        *,
        user,
        reason,
    ):
        if not self.pk:
            raise ValidationError(
                "未保存的流水不能冲销。"
            )

        reason = str(reason or "").strip()

        if not reason:
            raise ValidationError(
                "冲销流水必须填写原因。"
            )

        with transaction.atomic():
            locked = (
                type(self).objects
                .select_for_update()
                .select_related("account")
                .get(pk=self.pk)
            )

            if (
                locked.status
                == self.Status.REVERSED
            ):
                raise ValidationError(
                    "该流水已经冲销。"
                )

            locked.status = (
                self.Status.REVERSED
            )

            locked.reversal_reason = reason
            locked.reversed_at = timezone.now()
            locked.reversed_by = user

            locked.save(
                update_fields=[
                    "status",
                    "reversal_reason",
                    "reversed_at",
                    "reversed_by",
                    "updated_at",
                ]
            )

            self.refresh_from_db()

        return self

    def delete(self, *args, **kwargs):
        raise ValidationError(
            "收付款流水不能直接删除。"
            "请使用冲销操作。"
        )
