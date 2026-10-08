# Finance Operating Analysis V1 交付

分支：release/staging-20261005  
执行前及完成时 HEAD：c0e101c1657e781781b33b64b44817761178457b

## 文件与入口

修改：
- finance/views.py：新老板专属只读 view；原页面只新增入口显示标志。
- finance/urls.py：新增 finance namespace 路由。
- finance/templates/finance/settlement_dashboard.html：仅 Acoeurs 显示“公司经营分析”入口。

新增：
- finance/services/operating_finance_service.py：只读 EUR 月度聚合及上下文。
- finance/templates/finance/operating_dashboard.html、operating_chart.html：独立页面与图表片段。
- finance/static/finance/css/operating_dashboard.css、js/operating_dashboard.js：专属样式、双趋势图、图例切换和精确金额提示。
- finance/locale/zh_Hans/LC_MESSAGES/django.po、django.mo，以及对应法语目录：中法翻译；英语为源码标签。
- finance/test_operating_finance.py：30 项新测试。
- 本报告及 artifacts/operating_finance/ 下的合成浏览器验收截图。

URL：`/portal/finance/operations/?month=YYYY-MM`。Reverse：`portal:finance:operating_dashboard`。入口在原 `/portal/finance/` 页面，仅 Acoeurs 可见。Portal 首页、Claire/Cynthia 导航配置没有改动。

缺省为当前月份。空值、格式错误、重复月份或不能形成完整 12 个月日期区间的参数返回带明确错误的 400 页面。新页面只提供月份筛选；医院、工厂、订单、结算状态等旧参数不会改变公司整体统计范围。

## 数据和公式

全部正式金额计算为 Decimal，展示复用原 EUR 金额格式化。图表 JSON 保留 Decimal 字符串，只有浏览器绘图坐标转换为 Number；精确金额提示和月度表由服务端格式化。

| 指标 | 数据口径 |
|---|---|
| 医院开票销售额 | 未取消、EUR、receivable、正式 hospital_invoice 账户的冻结 original_amount，按 issue_date |
| 工厂采购额 | 未取消、EUR、payable、正式 factory_po 账户的冻结 original_amount，按 issue_date |
| 预计毛利润 | 医院开票销售额 − 工厂采购额；复用 calculate_accrual_totals |
| 预计毛利率 | 预计毛利润 ÷ 销售额；零销售显示“—” |
| 公司行政支出 | 未作废 AdministrativeExpense 的 amount，按 expense_date，包含所有分类 |
| 预计经营结余 | 预计毛利润 − 公司行政支出 |
| 预计经营结余率 | 预计经营结余 ÷ 销售额；零销售显示“—” |
| 医院登记收款 | 有效正式 EUR 应收账户上的 POSTED 流水，按 payment_date |
| 工厂登记付款 | 有效正式 EUR 应付账户上的 POSTED 流水，按 payment_date |
| 行政费用已付款 | 未作废、paid 的行政费用，按 paid_at |
| 净现金流估算 | 医院登记收款 − 工厂登记付款 − 行政费用已付款 |

复用 build_filtered_account_queryset 的 EUR 和取消账户规则，额外限定正式文档类型及匹配方向。不读取产品现价、Commercial PO 金额或 Demo 服务，不调整任何冻结金额。

四次聚合查询生成截至选择月份的连续 12 个月数据，按时间升序、空月份填零。行政费用聚合没有附件关联，因此多份附件不重复扣费。分类汇总提供五个一级及全部福利/报销二级分类、金额和占整体行政费用比例，并带月份跳转原老板分类页面。不选择姓名、工资说明或附件字段。

经营趋势：销售、采购、毛利润、行政支出、经营结余。现金趋势：医院收款、工厂付款、行政付款、净现金流估算。下方展开表提供所有月度数据。

现金流的账户集合没有 issue_date 筛选。9 月开立的 Invoice 在 10 月收到款项，计入 10 月现金流；9 月发生且 10 月支付的行政费用只扣减 9 月经营结余，在 10 月现金流扣减付款金额，不再扣减 10 月经营结余。即使账户开立日期早于 12 个月趋势起点，其区间内付款仍被统计。

## 权限与隔离

新 view 复用 boss_account_required，要求已登录、活跃且用户名精确为 Acoeurs；只接受 GET/HEAD。非老板、其他 Staff/Superuser、Claire、Cynthia、匿名以及 Demo 用户不能读取统计；权限检查在聚合之前执行。页面设置 private, no-store 和 nosniff。

原正式财务上下文只增加布尔入口标志，不添加行政费用金额。原 KPI、图表、订单表及 XLSX 导出内容保持不变。新页面只暴露公司汇总和分类入口，不提供工资附件链接或费用变更接口。

原结算和财务服务、导出服务、财务/行政模型、行政附件逻辑、老板身份模块、角色配置、首页导航及两项 Commercial Demo 服务的文件摘要在本轮前后完全一致。测试对原财务服务返回值、XLSX 单元格、Demo HTML 文本和 JSON 作行政费用变动前后比较，结果相同。

## 验证

| 检查 | 结果 |
|---|---|
| SQLite focused + 财务/权限/Demo/行政附件回归 | 148 项通过 |
| 隔离 PostgreSQL 正式 Finance/经营分析/行政费用/结算对比 | 94 项通过 |
| 新经营分析测试 | 30 项，包含在上述两组中 |
| Django system check | 通过，0 issues |
| Migration 一致性检查 | No changes detected |
| git diff --check | 通过 |
| Chromium 桌面与手机 | 入口、11 KPI、跨月值、双图、图例切换/提示、月份分类链接、中英法、390px 布局和非法月份 400 均通过 |
| 人工截图检查 | 中文经营页及法语手机页已查看 |

SQLite 回归：

```sh
.venv/bin/python manage.py test finance administrative_expenses portal.tests.test_role_isolation portal.tests.test_cynthia_navigation portal.tests.test_deployment_security portal.tests.test_settlement_home_comparison commercial_pos.tests.test_showcase --settings=config.settings_test
```

PostgreSQL 回归：

```sh
.venv/bin/python manage.py test finance administrative_expenses portal.tests.test_settlement_home_comparison --settings=config.settings_administrative_test_postgres
```

PostgreSQL 仅使用任务专用 /tmp 私有 Unix socket、65433 端口、新建并销毁测试库；测试结束后实例已停止。所有附件测试使用临时媒体目录。浏览器使用独立临时 SQLite/媒体和合成数据，不生成或重建 PDF。

**没有新增或修改模型及 Migration。** 没有 Commit、Push、部署 ECS、修改 Staging 或实际业务数据库，也没有覆盖原工作区未提交内容。

## git diff --stat

```text
 .../templates/finance/settlement_dashboard.html    |  3 +++
 finance/urls.py                                    |  1 +
 finance/views.py                                   | 29 ++++++++++++++++++++++
 3 files changed, 33 insertions(+)
```

默认 stat 只统计已跟踪文件；上述新增服务、测试、模板、资源、翻译和报告尚未暂存，不包含在此 stat 中。

## 限制

V1 只做公司整体 EUR 估算，不做按医院/订单分摊或独立经营分析导出。经营结余依赖当前登记的单据和费用，可能未完整覆盖税费、折旧等会计调整；行政付款状态为人工登记，净现金流估算并非银行对账现金余额。页面已说明这些口径。没有执行任何发布。
