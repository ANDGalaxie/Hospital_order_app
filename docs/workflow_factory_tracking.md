# Workflow / Factory Purchase / Shipment Tracking 验收报告

日期：2026-10-06。基于当前未提交 working tree 增量实现，无 commit、push 或 deploy。

1. **统一 BON ordinal**：Workflow、Factory Purchase 均复用 `portal.services.common.get_global_numeric_bon_ordinals`。未增加另一套计算逻辑。只对正式关联 Order 的纯数字 BON 全局排序；同 Order 每行均显示相同 ordinal，搜索/筛选不重新编号，非数字/未关联 Order 显示 —。只查询一份全局映射；批次/工作流关系 select_related，新增详情链接无逐行关联查询。
2. **Workflow 最终列**：BON 序号、订单号（下方继续显示批次）、医院、验证、Invoice、PO、下一步、发货日期、操作。空表 colspan=9。
3. **Workflow 发货日期**：精确读取 `DocumentWorkflowItem.shipment_batch.batch_date`，只显示 YYYY-MM-DD；不回退到 created_at / updated_at / generated time。
4. **默认排序**：`F("shipment_batch__batch_date").desc(nulls_last=True)`，其次 `-shipment_batch__batch_number`，最后 `-id`。现有 q、all/pending/docs/done、stats、生成动作、Invoice/PO PDF 与详情链接保持。
5. **null 日期**：显示 —，排在有日期记录后；SQLite 和 PostgreSQL 均已通过排序测试。同日及同批次号按 ID 稳定降序。批次标签读取 canonical batch_number，避免 validation JSON 缺失/过期使标签丢失。
6. **Factory 最终列**：BON 序号、订单号、工厂、发货日期、产品 / Serial、状态、下一步、操作。空表 colspan=8。原查看 Factory Confirmation 按钮保留。
7. **tracking 模型**：仅在 `shipments.models.ShipmentBatch` 增加 `tracking_number`，CharField(max_length=200, blank=True, default="", verbose_name="Tracking number")。无 carrier、物流 API、状态或 URL 等扩展字段。
8. **migration**：`shipments/migrations/0006_shipmentbatch_tracking_number.py`，只有一个 AddField，无 data migration。已在 SQLite、PostgreSQL 测试库及当前本地库应用。应用前确认计划只有这一个字段；64 个历史批次默认空字符串。迁移前后快照证实原订单、批次其他字段、工作流、工厂确认数据未变。
9. **tracking URL**：`/portal/shipments/<batch_id>/tracking/`；name=`portal:shipment_tracking_edit`。对象是 ShipmentBatch ID。
10. **表单**：`portal/forms/shipment_tracking_forms.py::ShipmentTrackingNumberForm` 为 ModelForm，仅 exposes tracking_number。支持自动 strip、常见物流字符、200 字符限制、修改和清空。View 采用 staff_member_required、GET/POST 和默认 CSRF 边界。保存仅 update_fields=["tracking_number"]，不更新批次 updated_at 或状态。成功消息和安全 next 跳转；取消回来源页，默认 Factory Purchase。
11. **填写/修改规则**：有已关联 Order 的 ShipmentBatch，空值显示“填写快递单号”，非空显示“修改快递单号”及原始号码。无 Order/无 Batch 不显示 tracking 链接。未提取、提取失败、待同步等提示继续保留。下一步中的操作竖向排列，操作列只保留 Factory Confirmation 查看。
12. **真实工作流链接**：“查看工作流”使用真实 anchor，href 为 `reverse("portal:workflow_detail", args=[item.pk])`。
13. **严格批次匹配**：新增 `get_batch_workflow_item(confirmation)`，只沿当前 confirmation.shipment_batch.document_workflow_item 获取；缺失时不产生工作流链接，显示合理同步提示。
14. **旧 fallback**：`get_workflow_item(confirmation)` 保留按订单取最新 Workflow 的兼容语义，供旧状态/详情显示使用。新增 next-action 详情链接不使用它，避免旧状态改动或串批次。
15. **多批次**：B1/B2/B3 每行分别链接其自身 WorkflowItem，已在单元测试和浏览器点击验证；B2 没 Workflow 时不借用 B3。
16. **下游影响**：tracking 为人工记录字段，无 validate/sync/generation 调用。保存测试比较订单、批次其他字段、Workflow、FactoryConfirmation 全量值，确认无变化。非空 tracking 下重跑 7 个 Invoice/PO 生成、编号复用、冻结批次和不一致 PO 拦截场景通过；工厂提取、发货历史、缺货和库存分配代表性回归通过。
17. **三语言**：复用既有 BON 序号、发货日期、查看工作流翻译；英法各新增 5 个 tracking 文案并 compilemessages。号码本身不翻译。测试与浏览器均覆盖中文、English、Français。
18. **CSS / responsive**：仅修改 Workflow / Factory scoped 样式。BON 辅助列窄且弱化；两张表列宽合计 100%；表头可换行；Factory 下一步有足够宽度、号码可换行。1440px 桌面无表格溢出或重叠；390px 下只有 table wrapper 横向滚动，body 宽度恰为 390px。tracking form 在窄屏正常。
19. **测试结果**：SQLite 全部 Portal + 工厂提取完整性/显式批次匹配 + 标准文档生成/工作流价格验证 + backorders：264 项全部通过。核心新文件共 43 项（36 项新增功能检查 + 7 项非空 tracking 的文档生成回归）。
20. **PostgreSQL**：核心文件 43 项全部通过，验证 nulls_last、同日稳定排序、批次链接、表单/权限、共享 ordinal、业务无副作用和 Invoice/PO。测试库自动应用本次 migration。
21. **静态收集**：已执行 collectstatic --noinput，2 个 CSS 文件更新、167 个未变；浏览器请 Ctrl + Shift + R。check、compilemessages、makemigrations --check --dry-run、git diff --check 均通过，最终 No changes detected。

## 修改文件

22. 本轮新增：
- `shipments/migrations/0006_shipmentbatch_tracking_number.py`
- `portal/forms/shipment_tracking_forms.py`
- `portal/shipment_tracking_views.py`
- `portal/templates/portal/shipments/tracking_edit.html`
- `portal/tests/test_workflow_factory_tracking.py`
- `docs/workflow_factory_tracking.md`（本报告）

本轮增量修改：
- `shipments/models.py`
- `portal/services/workflow_portal_service.py`
- `portal/services/factory_portal_service.py`
- `portal/urls.py`
- `portal/templates/portal/workflow/list.html`
- `portal/templates/portal/factory/list.html`
- `portal/static/portal/css/workflow.css`
- `portal/static/portal/css/factory.css`
- `locale/en/LC_MESSAGES/django.po` / `django.mo`
- `locale/fr/LC_MESSAGES/django.po` / `django.mo`

原有 Hospital Order、Settlement、Hospital Engagement 及 Team Activity 改动保留；没有修改这些既有功能的实现。outputs/ 下为 gitignored 的验证脚本、日志、截图和修改前快照。

23. 整个工作区 `git diff --stat`（包含此前任务的未提交改动，不包含未跟踪新文件）：

```text
 config/settings.py                           |   1 +
 locale/en/LC_MESSAGES/django.mo              | Bin 80745 -> 88889 bytes
 locale/en/LC_MESSAGES/django.po              | 508 +++++++++++++++++++++++++++
 locale/fr/LC_MESSAGES/django.mo              | Bin 87802 -> 96643 bytes
 locale/fr/LC_MESSAGES/django.po              | 508 +++++++++++++++++++++++++++
 portal/services/common.py                    |  17 +
 portal/services/factory_portal_service.py    |  53 ++-
 portal/services/home_portal_service.py       |  27 ++
 portal/services/order_portal_service.py      |   4 +
 portal/services/settlement_portal_service.py |  17 +-
 portal/services/workflow_portal_service.py   |  12 +-
 portal/static/portal/css/factory.css         |  50 ++-
 portal/static/portal/css/orders.css          |  40 ++-
 portal/static/portal/css/workflow.css        |  42 ++-
 portal/templates/portal/factory/list.html    |  16 +-
 portal/templates/portal/orders/list.html     |  11 +-
 portal/templates/portal/workflow/list.html   |  11 +-
 portal/urls.py                               |  16 +-
 shipments/models.py                          |   4 +
 19 files changed, 1262 insertions(+), 75 deletions(-)
```

## 视觉与本地验收证据

- `outputs/workflow_factory_visual/`：三语言 Workflow/Factory、tracking 填写/修改、Batch 2 Workflow detail、三张窄屏截图与 results.json。
- 浏览器在隔离 SQLite :memory: fixture server 中实际执行 CSRF 表单保存；B2 值被 strip 并保存、B1 原值保留、修改预填与取消正确、严格链接进入 Batch 2 detail。
- 浏览器脚本错误/静态资源错误为 0。
- 本地迁移后另外直接只读渲染实际数据库 Workflow、Factory、tracking GET 均返回 200，未向实际批次填写示例 tracking。
- `outputs/workflow_factory_regression_sqlite.log`
- `outputs/workflow_factory_new_postgres.log`
- `outputs/workflow_factory_local_migration.log`

未 commit / push / deploy。

