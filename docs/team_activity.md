# Team Activity 实现与验收报告

日期：2026-10-06。基于工作区原有未提交 Hospital Engagement 实现增量完成；未 commit / push。

## 访问边界与入口

1. 集中定义：`hospital_engagements/boss_access.py` 中 `BOSS_USERNAME = "Acoeur"`。
2. `is_boss_user(user)` 严格要求 authenticated、active、真实 username 精确匹配；不做大小写转换、前缀、昵称匹配。
3. 实现前通过 `get_user_model()` 只读检查实际数据库，找到 Acoeur：id=1，is_active=True，is_staff=True，is_superuser=True。未创建、修改、重命名或授予任何权限。
4. `portal/services/home_portal_service.py::build_home_context` 仅在 `is_boss_user` 为 True 时追加 Portal 一级卡片；图标复用 workflow，theme=violet。
5. Acoeur 可以看到卡片；没有 Hospital Engagement permission 也能直接访问老板页面。
6. acoeurs 看不到卡片，直接 URL 返回 403。
7. 其他 staff 看不到卡片；即使有医院沟通权限也返回 403。
8. 所有 Team Activity URL 由统一 `boss_account_required` 保护；匿名或 inactive 用户也拒绝。
9. 非 Acoeur superuser 同样返回 403，无超级用户绕过。
10. URL：`/portal/hospital-engagements/team-activity/`；URL name：`portal:team_activity`。View 只执行访问控制、GET 参数传递、service 与 render。
11. Team Activity 不使用 Django Permission / Group / Admin 权限或环境 allowlist；未新增 `view_team_activity`。普通 Hospital Engagement 权限实现保持原样。
12. 没有新增日报、周报、KPI 或任何业务数据表；全部基于既有 canonical data。

现有 Hospital Engagement 首页仍只有三个阶段卡片；首页、阶段页、detail 不添加老板入口。
Portal 首页保留原有 staff 入口约束；老板专用 URL 的判断本身不要求 staff。

## 统计语义

13. 今天为 Django 当前 timezone 的本地自然日 [当天零点, 次日零点)；本周为 [本地周一零点, now)；custom 为 [开始日零点, 结束日次日零点)。无硬编码 Paris，支持夏令时自然日；反向日期、缺日期、无效日期显示可翻译错误。
14. 统计与排序使用 `HospitalFollowUp.occurred_at`，不使用 created_at。
15. 联系医院：周期内 communication 的 distinct hospital。
16. 沟通次数：周期内 communication 条数，不含 stage_change / note。
17. 阶段前进：明确 rank mapping stage_1=1、stage_2=2、stage_3=3；只有目标 rank 大于原 rank 的 stage_change；空值、未知值、相同或后退不计。KPI 按医院去重，明细保留每次前进。
18. 进入第二/第三阶段：前进事件且 stage_to 为 stage_2 / stage_3 的 distinct hospital；同院多次前进不重复计入总医院数。
19. 工作归属为 FollowUp.created_by；当前逾期责任为 Engagement.owner。页面说明、service 注释和测试均区分两者。业务员候选只包括 active owner 或曾创建 FollowUp 的 active user。全部视图按记录人显示事实摘要，不排名/评分；历史 inactive/null 作者的工作仍保留在总览。
20. 逾期为 active hospital 且 next_follow_up_date < today；当天不逾期。逾期天数动态计算，不存库。始终是当前 snapshot，与所选周期无关。最近一次沟通仅查 communication，排序 occurred_at DESC、pk DESC。
21. service 集中使用过滤聚合、Count(distinct=True)、分组摘要、select_related、EXISTS 候选、Subquery 最近沟通；不逐业务员或逐医院查询。三张明细各自每页 30 条，筛选与其他表分页参数保留。已测试增加业务员/医院后整页查询数量不增长（上限 11）。
22. 中文源文案，English / Français gettext；分别追加 41 个缺失翻译并编译 .mo，保留已有翻译与原始业务数据。
23. 无模型修改、无新 migration；检查返回 No changes detected。

## 验证结果

24. 新增 37 项 Team Activity tests。
    - SQLite：`hospital_engagements + portal.tests` 总计 275 项，273 通过，2 项 PostgreSQL-only 并发测试跳过。
    - PostgreSQL：`hospital_engagements` 总计 117 项，全部通过，包含新 37 项和既有并发测试。
    - 覆盖老板/相似账号/staff/superuser/匿名/inactive、无普通权限访问、业务只读、时间边界、DST、补录、归属、前进/后退/非法阶段、去重、逾期、筛选、排序、分页、三语言、查询数量。
    - 全部 Portal 测试包含首页与 i18n 回归。
    - Django check、compilemessages、makemigrations --check --dry-run、git diff --check 均通过。
25. Visual smoke：现有 Chrome + Playwright，使用独立 SQLite :memory: 示例数据库；没有写入实际用户或业务数据。
    - 1440px 桌面三语言首页卡片与概览。
    - KPI 示例预期/实际均为 3 / 33 / 3 / 3 / 1。
    - 点击入口、分页、业务员筛选、custom 空周期、逾期快照不受周期影响、无效日期、语言菜单。
    - 390px 手机页面宽度为 390px，宽表区域内横向滚动，无整页溢出。
    - 浏览器脚本错误及静态资源错误均为 0。
    - 已人工查看桌面首屏、阶段/逾期明细、手机首屏及横向滚动截图。
    - 验证限于隔离数据的本机浏览器环境，未进行外部生产部署。
    - 截图与机器可读结果：`outputs/team_activity_visual/`；`results.json`。
    - 测试日志：`outputs/team_activity_tests_sqlite.log`、`outputs/team_activity_tests_postgres.log`。

## 本轮修改文件

26. 新增：
    - `hospital_engagements/boss_access.py`
    - `hospital_engagements/tests/test_team_activity.py`
    - `portal/services/team_activity_service.py`
    - `portal/team_activity_views.py`
    - `portal/templates/portal/team_activity/overview.html`
    - `portal/templates/portal/team_activity/pagination.html`
    - `portal/static/portal/css/team_activity.css`
    - `docs/team_activity.md`（本报告）

    增量修改：
    - `portal/services/home_portal_service.py`
    - `portal/urls.py`
    - `locale/en/LC_MESSAGES/django.po`
    - `locale/en/LC_MESSAGES/django.mo`
    - `locale/fr/LC_MESSAGES/django.po`
    - `locale/fr/LC_MESSAGES/django.mo`

    `outputs/` 下仅为 gitignored 的验证脚本、日志及截图；`staticfiles/` 下为 collectstatic 输出。
    未改现有医院沟通模型、阶段 service、业务员权限、contacts/departments/product interests/FollowUp 创建或订单财务流程；现有 config/settings.py 改动属于本轮开始前已有内容。

27. 当前整个工作区的 `git diff --stat`（包含此前未提交修改；不会显示未跟踪的新文件）：

```text
 config/settings.py                     |   1 +
 locale/en/LC_MESSAGES/django.mo        | Bin 80745 -> 88565 bytes
 locale/en/LC_MESSAGES/django.po        | 484 +++++++++++++++++++++++++++++++++
 locale/fr/LC_MESSAGES/django.mo        | Bin 87802 -> 96282 bytes
 locale/fr/LC_MESSAGES/django.po        | 484 +++++++++++++++++++++++++++++++++
 portal/services/home_portal_service.py |  27 ++
 portal/urls.py                         |  15 +-
 7 files changed, 1010 insertions(+), 1 deletion(-)
```

## 本地静态文件

已执行 `python manage.py collectstatic --noinput`：1 个新静态文件，168 个未变。
更新其他运行环境时仍需执行该命令；浏览器使用 **Ctrl + Shift + R** 强制刷新。
若现有 Django 进程未自动加载新 Python 路由，需按既有启动方式重启应用。

## 可复现命令

在工作区使用现有 .venv：

```sh
.venv/bin/python manage.py check
.venv/bin/python manage.py test hospital_engagements portal.tests --settings=config.settings_test --noinput
.venv/bin/python manage.py test hospital_engagements --keepdb --noinput
.venv/bin/python manage.py compilemessages --ignore=.venv
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python manage.py collectstatic --noinput
git diff --check
```

