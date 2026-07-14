from django.db import models


class FinanceDashboard(models.Model):
    """
    财务分析仪表盘的虚拟入口。
    这个模型主要用于让 Django Admin 左侧栏出现“财务分析”入口。
    """

    name = models.CharField(
        max_length=100,
        default="财务分析仪表盘",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "财务分析"
        verbose_name_plural = "财务分析"

    def __str__(self):
        return self.name