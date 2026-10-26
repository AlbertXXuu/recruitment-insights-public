# 数据来源与合成演示

此版本随附 180 条自生成模拟岗位及更新/导入模板；`scripts/seed_showcase.py --db <独立路径>` 另外生成两家演示企业各 1 条发布岗位和 1 条草稿，并准备包含只读 HR 的 8 个账号。不含第三方招聘原文。每条模拟岗位通过 `is_demo` 数据范围及正文明确标注；不可将它们改称真实企业招聘。

`scripts/seed_db.py --demo-only --db <独立路径>` 只导入现有合成 CSV，不写入真实来源，也不创建账号。`scripts/seed_db.py --fixtures-only --output-dir <目录>` 可以重新生成合成 CSV；默认种子可复现。

## 使用自己的数据

管理员或有维护权的企业成员可以从网页导入 CSV。列名与示例见 `data/import_template.csv`；企业身份由登录账号决定，不能通过 CSV 冒充其他企业。模拟内容应选择模拟数据；自有或已授权的真实内容才使用真实范围。

公开接口适配器、归一化、限流、溯源、更新、字段核验和重复处理能力均保留。`data/public_sources.json` 是接口元数据清单，包含 Greenhouse、Ashby、Lever、腾讯等适配目标；该文件不含岗位正文，接口可访问也不等于可以重新分发响应原文。

在确认自己有权使用后，可显式运行 `python scripts/collect_public_jobs.py --help` 查看采集选项，或 `python scripts/fetch_source.py` 读取单一来源。采集结果默认写到被 Git 忽略的 `data/public_jobs.json` 或 `data/cloudflare_snapshot.json`。不要把本地采集结果自动加入公开仓库。

管理员界面的“载入已授权来源快照”沿用 `data/public_jobs.json` 的既有结构：顶层为来源数组，每项包含 `source`（适配器前缀及来源名）、`label`、带时区的 `fetched_at` 和非空 `records`。请使用有权使用的真实来源文件；不要将合成记录伪装为真实来源。文件未准备时接口给出明确提示，既有数据不被清空。

本地默认启动没有周期同步（`--sync-interval` 默认为 0）。点击管理员“更新 Cloudflare 岗位”会主动联网；它不属于离线合成演示的必要步骤。接口可能随时间变化，测试使用合成响应，未把网络可用性当作稳定保证。

## 国内来源评估

先核对适用使用条件、稳定 ID、完整职责与任职要求、采集时间及来源链接，再做有限小样和重复导入核验。公开可读、内部研究可用和可以随开源项目再分发是不同范围；缺失学历/经验不能补成“不限”，缺失薪资不能补零。

2026-10-26 核对的 [腾讯招聘服务条款](https://careers.tencent.com/m/zh-cn/termsservice.html) 限制程序抓取；保留现有适配器不代表默认获得使用许可，本次没有继续联网采集腾讯。BOSS 直聘等有明确限制的平台也不作为默认采集来源。[小米官方招聘](https://hr.xiaomi.com/website/opportunities.html) 的有限技术样本不随发行版分发。[香港政府开放招聘数据](https://data.gov.hk/sc-data/dataset/hk-csb-csb-gov-vacancies/resource/399f7474-ba3d-48ab-a0fd-13a141b033d5) 应按其[使用条款](https://data.gov.hk/sc/terms-and-conditions)注明来源和权利归属；其地区、币种及资格要求须单独处理，不能将港币按人民币统计。
