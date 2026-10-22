# recruitment-insights

招聘数据分析与岗位协作应用。Python 标准库 HTTP 服务、SQLite 与原生 HTML/CSS/JavaScript，采用分层模块化单体架构。此版本使用自生成数据演示，不附第三方招聘原文、真实用户资料或既有部署凭据。

## 功能

- 求职者注册、登录、独立身份显示；岗位检索、图表筛选、导出、收藏和私人备注。
- 企业负责人创建/编辑/发布/下架岗位，管理本企业 HR 维护权；可维护与只读 HR 的写入权限实时生效。
- 企业、个人和公开数据隔离；管理员 CSV 导入、来源更新、质量检查、学历/经验核验与疑似重复处理。
- 求职者本人 PDF/DOCX 简历上传、替换、原件下载与持久保存；上限 2 MiB，仅本人可访问。
- SQLite 迁移、事务、备份恢复工具；可选 HTTPS 邀请部署控制台。

## 本地运行

需要 Python ≥3.11、支持 JSON 的 SQLite ≥3.38；PDF 结构校验依赖 `pypdf==6.21.2`。无需 Node.js 构建，也无需域名、Cloudflare 或已有数据库。获得此目录或克隆未来的公开仓库后，在项目根目录执行：

Windows PowerShell：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scripts/seed_db.py --demo-only --db outputs/local-demo/demo.sqlite3
.\.venv\Scripts\python.exe seed_demo_accounts.py --db outputs/local-demo/demo.sqlite3
.\.venv\Scripts\python.exe app.py --db outputs/local-demo/demo.sqlite3 --port 8765
```

macOS / Linux：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/seed_db.py --demo-only --db outputs/local-demo/demo.sqlite3
.venv/bin/python seed_demo_accounts.py --db outputs/local-demo/demo.sqlite3
.venv/bin/python app.py --db outputs/local-demo/demo.sqlite3 --port 8765
```

打开 <http://127.0.0.1:8765/login>。初始化导入 **180 条明确标注的合成岗位**；登录后选择“模拟 / 测试数据”查看。“真实 / 自有数据”初始为空。按 Ctrl+C 停止前台服务；若端口被占用，改用其他端口。

账号由上面的显式脚本准备，不在服务启动时自动创建。以下演示账号共用公开密码 `JobLens-Demo-2026!`，仅供独立本地演示：

| 账号 | 身份 |
|---|---|
| `admin` | 数据管理员 |
| `boss_a` / `boss_b` | 两个不同企业的负责人 |
| `hr_a` / `hr_b` | 两企业 HR，维护权由负责人控制 |
| `candidate_a` / `candidate_b` | 两个独立求职者 |

重复初始化保留已有账号和密码。需要体验注册时，可在登录页创建新的普通求职者；不能自行选择企业或管理员身份。企业岗位默认草稿，负责人发布后求职者才能查看；本地演示应保留“模拟 / 测试岗位”标记。

## 验证与工具

使用当前虚拟环境的 Python 运行：

```sh
python -m unittest discover -s tests
node --test tests/test_frontend.cjs tests/test_login.cjs tests/test_resume_frontend.cjs
```

Node.js 仅用于前端测试。3 项旧版 2.2 实现对照测试需要未随此版本分发的私有历史 checkpoint，缺失时明确跳过；当前迁移、事务和权限测试仍执行。macOS/Linux 命令与标准库入口兼容，但本次发行准备只在 Windows 实跑。

源码打包：`python scripts/build_delivery.py`，验证包：`python scripts/verify_delivery_zip.py`。仅打包 Git 跟踪且通过发行范围检查的文件，不附 `.git` 历史、运行数据库或下载材料。备份恢复使用 `python scripts/database_backup.py --help`。

## 数据、部署与许可

真实数据导入和来源适配器保留，但不会在默认初始化中联网采集。管理员只有在自行准备有权使用的数据后，才使用相应功能。详见 [数据来源与合成演示](docs/DATA_SOURCES.md)、[架构](docs/ARCHITECTURE.md)、[可选部署](docs/DEPLOYMENT.md) 和 [发行范围](docs/PUBLIC_EDITION.md)。

原创代码及原创合成材料采用 [MIT](LICENSE)。软件使用 MiSans 字体，其字体文件适用随附小米协议；pypdf 使用自己的许可证。详见 [第三方声明](THIRD_PARTY_NOTICES.md)。项目不包含投递、面试、AI 简历解析或付费外部服务功能。
