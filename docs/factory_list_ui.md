# 工厂采购列表 UI 统一验收报告

日期：2026-10-06。本轮为当前 working tree 上的列表/UI 增量修改；未 commit、push 或 deploy。

1. 最终结构：页面标题与副标题 → 顶部操作 → 四张 KPI → 搜索 panel → 独立圆角表格。
2. 复用现有 portal-page-header、portal-header-actions、portal-button、portal-panel、portal-table-wrap、portal-table。新增小型共享 list.css 提供与医院订单/工作流一致的 stat-card 和搜索样式；未修改两个参考页面。
3. 移除列表页旧 breadcrumb，以及“采购文件列表 / 共 N 份采购文件”额外大白卡与 panel-header。
4. Header 三个按钮依次为：上传工厂采购（primary）、返回首页（secondary）、Admin（secondary，指向 FactoryConfirmation 管理列表）。
5. KPI 为数据库全量计数，不以 rows 长度推断：
   - 全部采购文件：FactoryConfirmation Count(pk)。
   - 待提取：not_started、pending、processing；不含 failed。
   - 待处理：failed；未匹配 Order；或者 success 但当前 confirmation 没有精确对应的 Batch Workflow（包含缺 Batch / 缺 Workflow）。
   - 已进入工作流：当前 confirmation.shipment_batch.document_workflow_item 确实存在。
   这些卡片表示独立事实，可能重叠，例如尚未提取且未匹配 Order 同时进入待提取与待处理；不会修改每行原有 combined status 文案。
6. 工作流 KPI 与过滤严格按当前 Batch；同 Order 的其他 Workflow 不计入。Q predicates 同时用于数据库 KPI 和每行分类 annotation，避免两套判断分叉。
7. 搜索支持正式/展示订单号、未匹配记录的 detected BON，以及工厂名称；支持不区分大小写的部分匹配。没有改变 BON 匹配逻辑。
8. status=all/pending/needs_action/workflow 与 q 可组合；切换卡片保留 q，提交搜索保留 status，清除 q 保留 status。未知 status 安全回到 all。KPI 始终基于全量数据，采用工作流现有语义；搜索只影响表格。
9. 保留八列：BON 序号、订单号、工厂、发货日期、产品 / Serial、状态、下一步、操作；空表 colspan=8。工厂长名称保留 ellipsis 并新增完整 title。
10. BON ordinal 继续调用既有 get_global_numeric_bon_ordinals；不新增编号算法，未匹配 Order 的 detected BON 不获取正式序号。
11. 填写/修改 tracking、号码显示、编辑页面、安全 next 返回均保留。浏览器验证从组合筛选进入编辑并保存后，status/q 仍然保留。
12. “查看工作流”仍为真实 anchor，严格指向当前 Batch 的 Workflow detail。无对应 Workflow 显示同步提示，不回退到同 Order 的其他批次。
13. Factory 排序保持 -created_at、-id；没有改成发货日期排序。
14. 查询优化：一次全量 KPI 聚合，一次 global BON mapping，一次关联/Serial 聚合列表查询。Serial 总数、非空 product_code distinct 数均在 SQL 计算。展示 helper 支持传入已加载的 strict Workflow，详情默认兼容语义保留。
   - 测试证实由 10 条增至 30 条采购文件（含缺 Workflow 的批次），始终 3 次查询。
   - 本地真实 54 份采购文件：修改前 110 次，修改后 3 次。
   - 修改前后排序及原有可见 row 字段完全相同；隐藏 workflow_text/class 改为列表当前 Batch 语义。
15. 中文、English、Français 均使用 gettext；复用已有状态术语，各新增 3 个缺失文案，已编译 .mo。业务名称、号码、日期均保持原值。
16. 1280px 桌面全部八列可见、下一步与操作无重叠；390px 下表格 wrapper 内滚动，body 宽度为 390px。Header 操作可换行；KPI 和搜索在窄屏纵向排列。
17. 视觉冒烟使用隔离 SQLite :memory: 示例数据库、现有 Chrome/Playwright：
   - 实际渲染中文医院订单、工作流和工厂采购做对照。
   - 中文 Header 高度均 68px、KPI 高度 95px、搜索框高 42px；主要区域 spacing、圆角、字号与按钮风格一致。
   - English、Français 长文案正常。
   - 验证 KPI strict filter、status+q、清除保留状态、精确 Workflow 点击、tracking 保存与保留筛选、窄屏横向滚动。
   - 浏览器脚本及静态资源错误为 0；已人工查看中文及英法/窄屏截图。
   - 另使用本地真实数据直接只读渲染 Factory list 返回 200，未修改真实 tracking 或其他业务字段。
18. 已执行 collectstatic --noinput：2 个 CSS 更新（共享 list.css 和 factory.css），168 个未变。浏览器请 Ctrl + Shift + R。
19. 本任务没有模型修改、没有新增 migration；makemigrations --check --dry-run 返回 No changes detected。此前 tracking_number 的 0006 migration 保留，属于上一轮工作。
20. 测试：
   - 新增 FactoryListUITests 共 22 项。
   - SQLite 全部 Portal + 工厂提取/显式批次 + Workflow 标准生成/价格校验 + backorders：286 项全部通过。
   - PostgreSQL 新列表测试：22 项全部通过。
   - 覆盖 KPI、重叠事实、strict Batch、搜索组合、全球 ordinal、tracking/link、旧状态/详情 helper、Serial 聚合、3 次查询、三语言、只读数据保护。
   - tracking 编辑、Factory detail/upload/extraction、ShipmentBatch、Invoice/PO、库存与缺货代表性回归通过。
   - check、compilemessages、makemigrations drift check、git diff --check 通过。
   - AST 审计确认 18 个原有非列表函数未修改，包括提取、匹配、业务保存及 combined status；本地数据库只读前后快照一致。

## 修改文件

21. 本轮增量修改：
- portal/services/factory_portal_service.py
- portal/templates/portal/factory/list.html
- portal/static/portal/css/factory.css
- locale/en/LC_MESSAGES/django.po / django.mo
- locale/fr/LC_MESSAGES/django.po / django.mo

本轮新增：
- portal/static/portal/css/list.css
- portal/tests/test_factory_list_ui.py
- docs/factory_list_ui.md（本报告）

没有修改医院订单、工作流参考页面、tracking form/view、models、提取/生成业务或此前 OCR mitigation。outputs/ 为忽略跟踪的验证脚本、日志、截图和修改前快照。

22. 当前整个工作区 git diff --stat（包含此前未提交成果，不显示未跟踪新文件）：

```text
 config/settings.py                           |   1 +
 locale/en/LC_MESSAGES/django.mo              | Bin 80745 -> 89159 bytes
 locale/en/LC_MESSAGES/django.po              | 520 +++++++++++++++++++++++++++
 locale/fr/LC_MESSAGES/django.mo              | Bin 87802 -> 96959 bytes
 locale/fr/LC_MESSAGES/django.po              | 520 +++++++++++++++++++++++++++
 portal/services/common.py                    |  17 +
 portal/services/factory_portal_service.py    | 156 ++++++--
 portal/services/home_portal_service.py       |  27 ++
 portal/services/order_portal_service.py      |   4 +
 portal/services/settlement_portal_service.py |  17 +-
 portal/services/workflow_portal_service.py   |  12 +-
 portal/static/portal/css/factory.css         |  62 +++-
 portal/static/portal/css/orders.css          |  40 ++-
 portal/static/portal/css/workflow.css        |  42 ++-
 portal/templates/portal/factory/list.html    |  64 ++--
 portal/templates/portal/orders/list.html     |  11 +-
 portal/templates/portal/workflow/list.html   |  11 +-
 portal/urls.py                               |  16 +-
 shipments/models.py                          |   4 +
 19 files changed, 1416 insertions(+), 108 deletions(-)
```

## 验证产物

- outputs/factory_list_ui_regression.log
- outputs/factory_list_ui_postgres.log
- outputs/factory_list_ui_local_audit.json
- outputs/factory_list_ui_visual/results.json
- outputs/factory_list_ui_visual/reference-factory-zh.png
- outputs/factory_list_ui_visual/reference-orders-zh.png
- outputs/factory_list_ui_visual/reference-workflow-zh.png
- outputs/factory_list_ui_visual/factory-en.png
- outputs/factory_list_ui_visual/factory-fr.png
- outputs/factory_list_ui_visual/mobile-fr.png
- outputs/factory_list_ui_visual/mobile-table-right-fr.png

本地 KPI 实测：全部 54、待提取 0、待处理 0、当前 Batch 已进入工作流 54。

