# Finance PDF Export V1 交付

分支：release/staging-20261005  
HEAD：2dcd5a652ff98b4772e47d9277edb0f5ba8dff9a，执行前后未变。

## 接口及内容

| 接口 | 权限 | 报告 |
|---|---|---|
| /portal/finance/export.pdf | 与原正式财务页相同的 staff_member_required 及角色/Demo 隔离 | 财务实绩报告：筛选信息、10 个原 KPI、销售/采购与现金趋势、数字表、逾期及 30 天到期风险、订单文档与金额摘要 |
| /portal/finance/operations/export.pdf?month=YYYY-MM | boss_account_required，活跃且用户名精确为 Acoeurs | 内部公司经营报告：选定月份、11 个原 KPI、连续 12 个月双趋势及数字表、五类及全部二级行政汇总、财务口径说明 |

Reverse 为 `portal:finance:settlement_dashboard_export_pdf` 与 `portal:finance:operating_dashboard_export_pdf`。原财务页保留 XLSX，PDF 与 XLSX 并排；经营页 PDF 按钮携带网页已验证的月份。非法月份页面不显示导出按钮。两个下载接口只接受 GET/HEAD。

响应为 application/pdf、attachment、private, no-store、nosniff。按需在内存生成并返回；不存入 MEDIA_ROOT、不创建 GeneratedDocument、不写 Document Center，不重新生成 Invoice/PO。

## 计算、筛选及安全

- 实绩 PDF 调用原 `build_settlement_finance_dashboard_data`；KPI、订单金额、风险规则和原现金统计范围不变。使用 `parse_finance_filters` 保留 date_from/date_to/hospital/factory/order/status。日期范围按账户开立日期选账户，现金仍统计这些账户的有效流水并按付款日期分组。
- 经营 PDF 调用原 `build_operating_finance_context`；销售/采购按 issue_date，行政成本按 expense_date，行政付款按 paid_at，交易按 payment_date。9 月费用在 10 月支付和 9 月 Invoice 在 10 月收款的归属保留原经营服务规则。
- PDF 新接口额外拒绝无效结算状态、重复筛选参数，避免原解析器忽略错误后导出更大范围。经营导出复用原严格月份验证，包括重复、空值、非法格式及年份边界。原网页及 XLSX 解析逻辑没有改变。
- 正式金额和 KPI 直接使用服务返回的 Decimal/格式化值，不新建计算公式。实绩月度表和 SVG 使用该原 Dashboard 的趋势返回值，金额格式化与网页一致；绘图坐标使用浮点数，经营趋势表沿用服务端精确格式化金额。
- SVG 由服务端数值及 XML 安全转义标签生成；不是网页 Canvas 截图。支持正负值、全零、单月份、无数据提示，并保留对应数字表。
- WeasyPrint 只使用内联 SVG 与本地可信 CSS 文本，URL fetcher 拒绝网络、file 和 data 资源请求；不允许模板加载远程资源。
- 渲染后使用 PyMuPDF 验证真实 PDF 和页数；生成失败返回本地化、无敏感路径的 503，非法参数返回 400，不返回损坏 PDF。
- 公司报告只取行政分类与汇总金额，移除分类 URL，不包含员工姓名、工资说明、工资单、附件路径或附件链接。
- Cynthia 按原规则可使用实绩报告；Claire 和 Demo 不扩大权限。其他 Staff/Superuser 无法绕过老板导出。匿名或 inactive 用户被原服务端权限拦截。

原结算/经营/旧 Finance 服务、XLSX 服务、业务模型、行政附件逻辑、权限模块、Commercial Demo 两个服务、原图表 JS、requirements-docker.txt 和 Dockerfile 的内容摘要本轮前后完全相同。

## 打印范围及版式

A4 横版、白底、深蓝/灰色、清晰 KPI、红色负数、重复页眉页脚与页码、跨页重复表头。拉丁字符优先 DejaVu，中文回退 Noto CJK SC；没有下载或附带字体。

订单按原服务顺序最多打印 100 笔；两组风险清单各最多 50 笔。报告明确标注展示数、总数和上限。Invoice/PO 引用串超过 180 字符时以 [...] 明示缩写，完整明细和引用仍由原 XLSX 提供。所有 KPI 和趋势汇总不受这些打印上限影响。

订单身份/文档表与金额表按订单号分开呈现，避免 A4 中塞入过多列。经营与现金趋势分别有完整的 12 个月数值表；行政类别、二级类别及口径说明在独立页呈现。

## 文件清单

修改：
- finance/views.py：两种权限一致的导出 view、参数校验、安全下载及按钮 URL。
- finance/urls.py：两个 PDF route。
- finance/templates/finance/settlement_dashboard.html：并排 PDF/XLSX。
- finance/templates/finance/operating_dashboard.html：当前月份 PDF。
- finance/static/finance/css/settlement_dashboard.css：按钮分组/换行。
- finance/locale/zh_Hans/LC_MESSAGES/django.po、django.mo。
- finance/locale/fr/LC_MESSAGES/django.po、django.mo。

新增：
- finance/services/finance_pdf_export_service.py。
- finance/services/finance_pdf_charts.py。
- finance/static/finance/css/finance_report_pdf.css。
- finance/templates/finance/pdf/base.html。
- finance/templates/finance/pdf/kpis.html。
- finance/templates/finance/pdf/chart.html。
- finance/templates/finance/pdf/settlement_report.html。
- finance/templates/finance/pdf/operating_report.html。
- finance/test_pdf_export.py。
- docs/finance_pdf_export_v1.md。
- output/pdf/Acoeurs_Financial_Actuals_Sample.pdf。
- output/pdf/Acoeurs_Operating_Analysis_Sample.pdf。
- artifacts/finance_pdf/：渲染 PNG、其他语言 QA PDF 及字体/页数/时间元数据。

示例 PDF 完全由内存数据库中的合成记录生成，不含实际业务数据。临时验证脚本已移除。原工作区既有 artifacts、配置、商业展示文档和脚本保留。

## 测试与实际渲染

| 验证 | 结果 |
|---|---|
| 新 PDF focused tests | 24 项通过，最终打印样式上重跑通过 |
| SQLite 财务/XLSX/行政/权限/Demo 回归 | 165 项通过 |
| 隔离 PostgreSQL Finance/PDF/行政附件回归 | 111 项通过，临时库销毁，实例停止 |
| Django system check | 0 issues |
| makemigrations --check --dry-run | No changes detected |
| git diff --check | 通过 |
| 实际 Docker 镜像 | 已有 hospital_order_app-web:latest、WeasyPrint69；禁用网络，源码只读，内存 SQLite，临时媒体 |
| 中英法容器报告 | 六份均可正常打开，Noto CJK/DejaVu 嵌入，欧元、法语重音、中文和负数正常 |
| Poppler 逐页渲染 | 两份中文报告全部页及法语封面已查看；图表、页码、分类与金额表清楚，无文字挤出的近空白页 |

最终合成中文样例：财务实绩 5 页约 279 KB；经营分析 4 页约 329 KB。SVG 以真实向量线段和节点进入 PDF，测试检查可见绘图对象，既非空占位也非浏览器截图。

SQLite 回归：

```sh
.venv/bin/python manage.py test finance administrative_expenses portal.tests.test_role_isolation portal.tests.test_cynthia_navigation portal.tests.test_deployment_security commercial_pos.tests.test_showcase --settings=config.settings_test
```

PostgreSQL 回归：

```sh
.venv/bin/python manage.py test finance administrative_expenses --settings=config.settings_administrative_test_postgres
```

本地测试仅复用现有临时中文字体配置；容器实际验证使用 Dockerfile 已安装的 Noto CJK/DejaVu。没有新增依赖或 Migration。

## git diff --stat 与未跟踪文件

```text
 finance/locale/fr/LC_MESSAGES/django.mo            | Bin 4935 -> 9171 bytes
 finance/locale/fr/LC_MESSAGES/django.po            | 141 +++++++++++++++++++++
 finance/locale/zh_Hans/LC_MESSAGES/django.mo       | Bin 4314 -> 8111 bytes
 finance/locale/zh_Hans/LC_MESSAGES/django.po       | 141 +++++++++++++++++++++
 .../static/finance/css/settlement_dashboard.css    |   3 +
 finance/templates/finance/operating_dashboard.html |   2 +-
 .../templates/finance/settlement_dashboard.html    |   3 +
 finance/urls.py                                    |   2 +
 finance/views.py                                   |  64 ++++++++++
 9 files changed, 355 insertions(+), 1 deletion(-)
```

默认 stat 不含尚未暂存的新增文件，新增源文件完整列在上面的“新增”清单。既有未跟踪内容不属于本次改动：
artifacts 下其他目录、config/settings_administrative_test_postgres.py、config/settings_commercial_preview.py、config/settings_commercial_test_postgres.py、docs/administrative_expenses_v1.md、docs/deployment/commercial_demo.md、docs/deployment/commercial_demo_nginx.conf.example、scripts/commercial_po_acceptance.py。

## 限制

生成仍为同步请求，原统计服务仍读取所选范围的完整数据；打印限制控制报告篇幅，不改变原查询或汇总。此次镜像的合成样例测得中文生成约 20-23 秒；纯拉丁经营报告约 1.6-1.8 秒，含中文医院名称的英法实绩报告约 8 秒。耗时主要受字体与运行资源影响，本轮未改动服务超时、ECS 配置或部署。

没有 Commit、Push、ECS 部署、Staging 数据库或历史业务数据修改，也没有修改既有 Invoice/PO 文件。
