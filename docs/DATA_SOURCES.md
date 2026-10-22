# 数据来源与合成演示

此版本只随附 180 条自生成模拟岗位及更新/导入模板，不含第三方招聘原文。每条模拟岗位通过 `is_demo` 数据范围及正文明确标注；不可将它们改称真实企业招聘。

`scripts/seed_db.py --demo-only --db <独立路径>` 只导入现有合成 CSV，不写入真实来源，也不创建账号。`scripts/seed_db.py --fixtures-only --output-dir <目录>` 可以重新生成合成 CSV；默认种子可复现。

## 使用自己的数据

管理员或有维护权的企业成员可以从网页导入 CSV。列名与示例见 `data/import_template.csv`；企业身份由登录账号决定，不能通过 CSV 冒充其他企业。模拟内容应选择模拟数据；自有或已授权的真实内容才使用真实范围。

公开接口适配器、归一化、限流、溯源、更新、字段核验和重复处理能力均保留。`data/public_sources.json` 是接口元数据清单，包含 Greenhouse、Ashby、Lever、腾讯等适配目标；该文件不含岗位正文，接口可访问也不等于可以重新分发响应原文。

在确认自己有权使用后，可显式运行 `python scripts/collect_public_jobs.py --help` 查看采集选项，或 `python scripts/fetch_source.py` 读取单一来源。采集结果默认写到被 Git 忽略的 `data/public_jobs.json` 或 `data/cloudflare_snapshot.json`。不要把本地采集结果自动加入公开仓库。

管理员界面的“载入已授权来源快照”沿用 `data/public_jobs.json` 的既有结构：顶层为来源数组，每项包含 `source`（适配器前缀及来源名）、`label`、带时区的 `fetched_at` 和非空 `records`。请使用有权使用的真实来源文件；不要将合成记录伪装为真实来源。文件未准备时接口给出明确提示，既有数据不被清空。

本地默认启动没有周期同步（`--sync-interval` 默认为 0）。点击管理员“更新 Cloudflare 岗位”会主动联网；它不属于离线合成演示的必要步骤。接口可能随时间变化，测试使用合成响应，未把网络可用性当作稳定保证。
