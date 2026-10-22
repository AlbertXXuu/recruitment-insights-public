# 可选部署

合成演示只需 README 中的 loopback 服务，不运行 Tunnel 或 DNS 配置。公开服务应使用自行准备的账号与独立数据库，不能使用公开演示密码。

直接启动 HTTPS 反向代理源站时，使用自己的精确 HTTPS 域名：

```sh
python app.py --db /absolute/path/to/independent.sqlite3 --test-origin https://recruitment.example.org --port 8775 --allow-candidate-registration
```

`--test-origin` 要求 Python ≥3.11、SQLite ≥3.51.3 和 JSON 功能；新建数据库可直接初始化，已有数据库须停止其他连接并完成 checkpoint 后按预检要求启动。默认不开放公网注册，只有显式参数或控制台策略启用后可注册求职者。企业与管理员使用 `scripts/provision_test_account.py --help` 查看受控准备方式，不通过网页自助注册获得这些身份。

如选用 Cloudflare Tunnel，控制台和配置工具保留，身份通过 `scripts/deployment_settings.py` 统一读取以下环境变量，默认都是无可用身份的示例值：

| 变量 | 含义 |
|---|---|
| `RECRUITMENT_CF_ZONE` | 自己管理的根域 |
| `RECRUITMENT_CF_HOSTNAME` | 招聘站精确主机名；默认前缀加根域 |
| `RECRUITMENT_CF_ACCOUNT` | 自己的 Cloudflare account ID |
| `RECRUITMENT_CF_TUNNEL_ID` | 自己准备的独立 Tunnel ID |

这些变量只放非密钥标识；Token 仍通过原有交互入口读取，不写进源码或变量示例。所有相关进程须使用同一组身份值，不能在运行中更换。配置助手的账号、Zone、Tunnel 和 DNS 操作范围随这组值固定，不放宽为任意 API 代理。

Windows PowerShell 设置非密钥标识后，先配置受支持的解释器（运行时目录中的 `python-path.txt`，由控制台检查），再按自己准备的官方 cloudflared 二进制及固定摘要、运行配置和 Token 操作。使用 `python scripts/invitation_console.py --help`、`python scripts/configure_cloudflare.py --help` 查看入口。示例值无法连接维护者部署；本发行版不附服务凭据、域名配置或源站数据库。可选代理仅在自己的运行配置明确设置时使用，不是本地演示前提。

本版本验证了合成环境中的配置边界和角色业务；未替使用者创建真实域名、Tunnel、证书或公网服务，也未重新进行公网负载验收。正常停止服务后再维护数据库；备份恢复命令见 `python scripts/database_backup.py --help`。
