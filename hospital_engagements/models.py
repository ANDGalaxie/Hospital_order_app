from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


ACCESS_PERMISSION = "hospital_engagements.access_hospital_engagement"


class HospitalEngagement(models.Model):
    class Stage(models.TextChoices):
        FIRST = "stage_1", _("第一阶段 · 未接触")
        SECOND = "stage_2", _("第二阶段 · 接触中")
        THIRD = "stage_3", _("第三阶段 · 落地中")

    class Priority(models.TextChoices):
        HIGH = "A", _("A · 高")
        MEDIUM = "B", _("B · 中")
        LOW = "C", _("C · 低")

    hospital = models.OneToOneField(
        "hospitals.Hospital", on_delete=models.CASCADE, related_name="engagement"
    )
    stage = models.CharField(_("阶段"), max_length=20, choices=Stage.choices, default=Stage.FIRST)
    priority = models.CharField(_("优先级"), max_length=1, choices=Priority.choices, default=Priority.MEDIUM)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name=_("负责人"), null=True, blank=True,
        on_delete=models.SET_NULL, related_name="hospital_engagements",
    )
    demand_summary = models.TextField(_("需求摘要"), blank=True)
    special_requirements = models.TextField(_("特殊需求"), blank=True)
    next_action = models.TextField(_("下一步"), blank=True)
    next_follow_up_date = models.DateField(_("下次跟进日期"), null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["priority", "hospital__name", "pk"]
        indexes = [models.Index(fields=["stage", "priority"], name="he_stage_priority_idx")]
        permissions = [("access_hospital_engagement", "Can access Hospital Engagement")]

    def __str__(self):
        return str(self.hospital)


class HospitalDepartment(models.Model):
    hospital = models.ForeignKey(
        "hospitals.Hospital", on_delete=models.CASCADE, related_name="engagement_departments"
    )
    name = models.CharField(_("科室名称"), max_length=255)
    notes = models.TextField(_("备注"), blank=True)
    is_active = models.BooleanField(_("启用"), default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "pk"]
        constraints = [
            models.UniqueConstraint(
                Lower("name"), "hospital", condition=models.Q(is_active=True),
                name="he_active_department_name_unique",
            )
        ]

    def __str__(self):
        return self.name


class HospitalContact(models.Model):
    class ContactType(models.TextChoices):
        DOCTOR = "doctor", _("医生")
        PURCHASING = "purchasing", _("采购人员")
        ADMIN = "admin", _("行政人员")
        NURSE = "nurse", _("护士")
        OTHER = "other", _("其他联系人")

    hospital = models.ForeignKey(
        "hospitals.Hospital", on_delete=models.CASCADE, related_name="engagement_contacts"
    )
    department = models.ForeignKey(
        HospitalDepartment, verbose_name=_("科室"), null=True, blank=True,
        on_delete=models.SET_NULL, related_name="contacts",
    )
    name = models.CharField(_("姓名"), max_length=255)
    contact_type = models.CharField(_("联系人类型"), max_length=20, choices=ContactType.choices, default=ContactType.OTHER)
    title = models.CharField(_("职位"), max_length=255, blank=True)
    email = models.EmailField(_("邮箱"), blank=True)
    phone = models.CharField(_("电话"), max_length=100, blank=True)
    is_primary = models.BooleanField(_("主要联系人"), default=False)
    notes = models.TextField(_("备注"), blank=True)
    is_active = models.BooleanField(_("启用"), default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-is_primary", "name", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["hospital"], condition=models.Q(is_active=True, is_primary=True),
                name="he_one_active_primary_contact",
            )
        ]

    def clean(self):
        super().clean()
        if self.department_id and self.department.hospital_id != self.hospital_id:
            raise ValidationError({"department": _("科室必须属于当前医院。")})

    def __str__(self):
        return self.name


class HospitalProductInterest(models.Model):
    engagement = models.ForeignKey(HospitalEngagement, on_delete=models.CASCADE, related_name="product_interests")
    department = models.ForeignKey(
        HospitalDepartment, verbose_name=_("科室"), null=True, blank=True,
        on_delete=models.SET_NULL, related_name="product_interests",
    )
    product = models.ForeignKey(
        "products.Product", verbose_name=_("产品"), null=True, blank=True,
        on_delete=models.SET_NULL, related_name="hospital_interests",
    )
    product_text = models.CharField(_("自由文本需求"), max_length=500, blank=True)
    notes = models.TextField(_("说明"), blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-pk"]

    def clean(self):
        super().clean()
        if not self.product_id and not self.product_text.strip():
            raise ValidationError(_("请至少选择一个产品或填写自由文本需求。"))
        if self.department_id and self.department.hospital_id != self.engagement.hospital_id:
            raise ValidationError({"department": _("科室必须属于当前医院。")})

    def __str__(self):
        return str(self.product) if self.product_id else self.product_text


class HospitalFollowUp(models.Model):
    class ActivityType(models.TextChoices):
        COMMUNICATION = "communication", _("沟通记录")
        STAGE_CHANGE = "stage_change", _("阶段变更")
        NOTE = "note", _("备注")

    class Channel(models.TextChoices):
        PHONE = "phone", _("电话")
        EMAIL = "email", _("Email")
        WHATSAPP = "whatsapp", _("WhatsApp")
        VISIT = "visit", _("拜访")
        MEETING = "meeting", _("会议")
        OTHER = "other", _("其他")

    engagement = models.ForeignKey(HospitalEngagement, on_delete=models.CASCADE, related_name="follow_ups")
    activity_type = models.CharField(max_length=20, choices=ActivityType.choices, default=ActivityType.COMMUNICATION)
    contact = models.ForeignKey(
        HospitalContact, verbose_name=_("联系人"), null=True, blank=True,
        on_delete=models.SET_NULL, related_name="follow_ups",
    )
    department = models.ForeignKey(
        HospitalDepartment, verbose_name=_("科室"), null=True, blank=True,
        on_delete=models.SET_NULL, related_name="follow_ups",
    )
    occurred_at = models.DateTimeField(_("沟通时间"), default=timezone.now)
    channel = models.CharField(_("沟通方式"), max_length=20, choices=Channel.choices, blank=True)
    summary = models.TextField(_("沟通内容"), blank=True)
    outcome = models.TextField(_("沟通结果"), blank=True)
    next_action = models.TextField(_("下一步"), blank=True)
    next_follow_up_date = models.DateField(_("下次跟进日期"), null=True, blank=True)
    stage_from = models.CharField(max_length=20, choices=HospitalEngagement.Stage.choices, blank=True)
    stage_to = models.CharField(max_length=20, choices=HospitalEngagement.Stage.choices, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-occurred_at", "-pk"]
        indexes = [
            models.Index(fields=["engagement", "activity_type", "-occurred_at"], name="he_latest_communication_idx")
        ]

    def clean(self):
        super().clean()
        hospital_id = self.engagement.hospital_id
        if self.contact_id and self.contact.hospital_id != hospital_id:
            raise ValidationError({"contact": _("联系人必须属于当前医院。")})
        if self.department_id and self.department.hospital_id != hospital_id:
            raise ValidationError({"department": _("科室必须属于当前医院。")})
        if self.activity_type == self.ActivityType.COMMUNICATION:
            if not self.channel:
                raise ValidationError({"channel": _("请选择沟通方式。")})
            if not self.summary.strip():
                raise ValidationError({"summary": _("请填写沟通内容。")})
