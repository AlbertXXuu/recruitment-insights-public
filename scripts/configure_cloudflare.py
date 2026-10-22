"""Configure only the approved invitation hostname; API token stays in memory.

cloudflare-setup.cmd prepares only local runtime credentials (--runtime-only):
it does not need a management API token or verify cloud routes/DNS. Explicit
automatic configuration uses a short-lived management token. Neither mode uses
browser credentials, global API keys, account certificates, or a shared tunnel.
"""
import argparse
import base64
import csv
import getpass
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.invitation_console import ConsoleLock, RUNTIME_DIRECTORY, reject_links, validate_runtime
from scripts.invitation_tunnel import validate_tunnel_token

from scripts.deployment_settings import ACCOUNT, HOSTNAME, TUNNEL_ID, ZONE
NAME = 'recruitment-insights'
EXE_SHA256 = '12c7c887f8aaab779aef1dfa9b60673530a5adb88ecc55dd6df8f56e0b70f199'
BASE = 'https://api.cloudflare.com/client/v4'
VERIFY_PATH = '/user/tokens/verify'


class SetupError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward the Authorization header to another URL.


def endpoint_label(path):
    """Describe only known operations, never reflect arbitrary path text."""
    if path == VERIFY_PATH:
        return '临时 Token 验证'
    if path == '/zones':
        return '域名列表'
    prefix = f'/accounts/{ACCOUNT}'
    if path.startswith(prefix + '/cfd_tunnel/' + TUNNEL_ID):
        return '独立 Tunnel 及配置'
    if re.fullmatch(r'/zones/[^/]+/dns_records', path):
        return 'DNS 记录'
    return '受限资源'


def error_diagnostic(data):
    if not isinstance(data, dict):
        return '非标准 API 响应；未提供可用数字错误码'
    codes = []
    rows = data.get('errors')
    for row in rows if isinstance(rows, list) else []:
        code = row.get('code') if isinstance(row, dict) else None
        if type(code) is int and 1 <= code <= 999999 and code not in codes:
            codes.append(code)
            if len(codes) == 5:
                break
    numbers = ', '.join(map(str, codes)) if codes else '未提供可用数字错误码'
    return 'JSON API 响应；错误码：' + numbers


def verify_api_token(client):
    result = client.get(VERIFY_PATH)
    if not isinstance(result, dict) or result.get('status') != 'active':
        raise SetupError('临时 Token 未确认 active，请核对复制内容及有效期；未执行资源配置')


class Client:
    def __init__(self, token):
        self._token = token
        self._opener = build_opener(NoRedirect())
        self.zone_id = None  # Bound only after the fixed account and zone are verified.

    def call(self, method, path, payload=None, **query):
        if path == VERIFY_PATH:
            if method != 'GET' or payload is not None or query:
                raise SetupError('临时 Token 验证只允许无参数的只读 GET')
        else:
            tunnel_path = f'/accounts/{ACCOUNT}/cfd_tunnel/{TUNNEL_ID}'
            allowed = False
            if path == '/zones':
                allowed = (method == 'GET' and payload is None and
                           set(query) <= {'name', 'account.id', 'page', 'per_page'} and
                           query.get('name', ZONE) == ZONE and
                           query.get('account.id', ACCOUNT) == ACCOUNT)
            elif path in (tunnel_path, tunnel_path + '/token'):
                allowed = method == 'GET' and payload is None and not query
            elif path == tunnel_path + '/configurations':
                allowed = not query and (
                    (method == 'GET' and payload is None) or
                    (method == 'PUT' and payload == {'config': expected_config()}))
            elif self.zone_id and path == f'/zones/{self.zone_id}/dns_records':
                if method == 'GET':
                    allowed = (payload is None and set(query) <= {'name', 'page', 'per_page'} and
                               query.get('name') in (HOSTNAME, ZONE, 'www.' + ZONE))
                elif method == 'POST':
                    allowed = not query and payload == expected_dns()
            if not allowed:
                raise SetupError('API 请求超出本次固定账号、Zone 或独立 Tunnel 配置范围')
        url = BASE + path + ('?' + urlencode(query) if query else '')
        body = json.dumps(payload).encode() if payload is not None else None
        request = Request(url, data=body, method=method, headers={
            'Authorization': 'Bearer ' + self._token,
            'Content-Type': 'application/json', 'User-Agent': 'recruitment-insights-setup'})
        try:
            with self._opener.open(request, timeout=30) as response:
                data = json.loads(response.read(4 * 1024 * 1024))
        except HTTPError as exc:
            # Read a bounded error envelope, retaining only numeric codes.
            # Never echo messages, headers, URL, token, or resource results.
            try:
                error_data = json.loads(exc.read(16 * 1024))
            except Exception:
                error_data = None
            raise SetupError(f'Cloudflare API {method} 请求失败（{endpoint_label(path)}）：'
                             f'HTTP {exc.code}；{error_diagnostic(error_data)}。'
                             '请按失败接口核对授权或网络；未自动重试。'
                             '若是写入请求，重开助手先核对当前状态。') from None
        except (URLError, OSError, ValueError):
            raise SetupError(f'Cloudflare API 连接或响应失败（{endpoint_label(path)}）；'
                             '不自动重试写入，重开助手会先核对现状') from None
        if not isinstance(data, dict) or not data.get('success') or 'result' not in data:
            raise SetupError(f'Cloudflare API 未确认成功（{endpoint_label(path)}）；'
                             f'{error_diagnostic(data)}；不打印原始响应，请在面板核对')
        return data

    def get(self, path, **query):
        return self.call('GET', path, **query)['result']

    def items(self, path, **query):
        result = []
        for page in range(1, 101):
            data = self.call('GET', path, page=page, per_page=50, **query)
            rows = data['result']
            if not isinstance(rows, list):
                raise SetupError('列表响应格式不符')
            result.extend(rows)
            info = data.get('result_info') or {}
            if not isinstance(info, dict):
                raise SetupError('列表分页信息格式不符')
            pages = info.get('total_pages')
            if isinstance(pages, int) and not isinstance(pages, bool) and pages >= 1:
                if page >= pages:
                    return result
            elif not rows:
                return result
        raise SetupError('列表超出本次检查范围，请在 Cloudflare 面板核对')


def expected_config():
    return {'ingress': [
        {'hostname': HOSTNAME, 'service': 'http://127.0.0.1:8775', 'originRequest': {
            'httpHostHeader': HOSTNAME, 'disableChunkedEncoding': True}},
        {'service': 'http_status:404'}]}


def expected_dns():
    return {'type': 'CNAME', 'name': HOSTNAME, 'proxied': True,
            'content': TUNNEL_ID + '.cfargotunnel.com', 'ttl': 1}


def matching_dns(records):
    return (len(records) == 1 and isinstance(records[0], dict) and
            records[0].get('name') == HOSTNAME and records[0].get('type') == 'CNAME' and
            records[0].get('content') == TUNNEL_ID + '.cfargotunnel.com' and
            records[0].get('proxied') is True)


def canonical(records):
    if (not isinstance(records, list) or
            any(not isinstance(row, dict) or not isinstance(row.get('id'), str) for row in records)):
        raise SetupError('DNS 列表响应格式不符')
    return json.dumps(sorted(records, key=lambda row: row['id']), sort_keys=True)


def preflight(client):
    prefix = f'/accounts/{ACCOUNT}'
    zones = client.items('/zones', name=ZONE, **{'account.id': ACCOUNT})
    if (len(zones) != 1 or not isinstance(zones[0], dict) or
            zones[0].get('name') != ZONE or
            not isinstance(zones[0].get('account'), dict) or
            zones[0]['account'].get('id') != ACCOUNT or
            not isinstance(zones[0].get('id'), str) or
            not re.fullmatch(r'[a-zA-Z0-9-]{1,64}', zones[0]['id'])):
        raise SetupError('未唯一找到目标账号的 example.com Zone')
    zone_id = zones[0]['id']
    client.zone_id = zone_id
    tunnel = client.get(prefix + '/cfd_tunnel/' + TUNNEL_ID)
    if (not isinstance(tunnel, dict) or tunnel.get('id') != TUNNEL_ID or
            tunnel.get('name') != NAME or tunnel.get('config_src') != 'cloudflare' or
            tunnel.get('deleted_at')):
        raise SetupError('独立 Tunnel 的 ID、名称、管理方式或状态不符')
    dns_path = f'/zones/{zone_id}/dns_records'
    records = client.items(dns_path, name=HOSTNAME)
    if records and not matching_dns(records):
        raise SetupError('目标域名已有不同 DNS 记录；助手不会删除或覆盖')
    baseline = {name: canonical(client.items(dns_path, name=name))
                for name in (ZONE, 'www.' + ZONE)}
    return zone_id, records, baseline


def restrict_token_file(path):
    if os.name == 'nt':
        result = subprocess.run(['whoami', '/user', '/fo', 'csv', '/nh'],
                                capture_output=True, text=True, check=True)
        sid = list(csv.reader(result.stdout.strip().splitlines()))[-1][-1]
        if not re.fullmatch(r'S-1-[0-9-]+', sid):
            raise SetupError('无法确认当前 Windows 账号 SID')
        encoded_path = base64.b64encode(str(path).encode('utf-8')).decode('ascii')
        # Install one protected DACL without temporarily restoring inheritance
        # on an existing file containing a secret. Only non-secret data is in argv.
        script = "$ErrorActionPreference='Stop'; " + (
            f"$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded_path}')); "
            "$a=[Security.AccessControl.FileSecurity]::new(); "
            "$a.SetAccessRuleProtection($true,$false); "
            f"$s=[Security.Principal.SecurityIdentifier]::new('{sid}'); "
            "$r=[Security.AccessControl.FileSystemAccessRule]::new($s,'FullControl','Allow'); "
            "$a.AddAccessRule($r); [IO.File]::SetAccessControl($p,$a)")
        command = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
        subprocess.run(['powershell.exe', '-NoProfile', '-EncodedCommand', command],
                       capture_output=True, check=True, timeout=15)
    else:
        os.chmod(path, 0o600)


def secure_token_file(path, token):
    reject_links(path)
    if path.exists():
        restrict_token_file(path)
        if path.read_text(encoding='utf-8').strip() != token:
            raise SetupError('本地已有不同 Tunnel Token；不覆盖，请核对是否发生轮换')
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    reject_links(temporary)
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            restrict_token_file(temporary)
            stream.write(token)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)  # Atomically publish without overwriting another file.
    finally:
        temporary.unlink(missing_ok=True)


def check_runtime_metadata(directory, runtime):
    destination = directory / 'tunnel-runtime.json'
    reject_links(destination)
    if destination.exists():
        try:
            if destination.stat().st_size > 16 * 1024:
                raise ValueError
            current = json.loads(destination.read_text(encoding='utf-8'))
        except (ValueError, OSError):
            raise SetupError('本地运行配置无法确认；不覆盖，请核对') from None
        if current != runtime:
            raise SetupError('本地已有不同或旧 Access 运行配置；不覆盖，请先核对')


def local_runtime(directory):
    directory = Path(directory)
    reject_links(directory)
    binary = directory / 'tools/cloudflared.exe'
    reject_links(binary)
    if not binary.is_file() or hashlib.sha256(binary.read_bytes()).hexdigest() != EXE_SHA256:
        raise SetupError('本机 cloudflared 缺失或 SHA256 不符，请先准备官方 2026.10.2 程序')
    runtime = {'tunnel_id': TUNNEL_ID, 'name': NAME, 'hostname': HOSTNAME,
               'authentication': 'application', 'cloudflared_sha256': EXE_SHA256}
    check_runtime_metadata(directory, runtime)
    return directory, runtime


def save_runtime(directory, runtime, token):
    try:
        validate_tunnel_token(token)
    except ValueError:
        raise SetupError('Tunnel 运行 Token 的格式、账号或独立 Tunnel 身份不符；未保存凭据') from None
    check_runtime_metadata(directory, runtime)
    secure_token_file(directory / 'tunnel-token.txt', token)
    destination = directory / 'tunnel-runtime.json'
    if destination.exists():
        check_runtime_metadata(directory, runtime)
        return
    temporary = directory / ('tunnel-runtime.' + uuid.uuid4().hex + '.tmp')
    reject_links(temporary)
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(runtime, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, destination)  # Publish atomically without replacing a concurrent file.
    finally:
        temporary.unlink(missing_ok=True)


def prepare_runtime(token, directory=RUNTIME_DIRECTORY):
    directory, runtime = local_runtime(directory)
    save_runtime(directory, runtime, token)
    print('本机 Tunnel 运行凭据已准备；使用应用账号登录。')
    print('未验证云路由或 DNS；请在网页核对精确域名到 http://127.0.0.1:8775 的路由和代理 CNAME。')
    print('仍须启动独立测试库，并完成真实 HTTPS 登录、角色权限及写入验收。')
    return runtime


def remote_config(response):
    if response is not None and not isinstance(response, dict):
        raise SetupError('独立 Tunnel 配置响应格式不符')
    current = (response or {}).get('config')
    if current is None:
        return {}  # A successful null result/config means no configuration, unlike HTTP 404.
    if not isinstance(current, dict):
        raise SetupError('独立 Tunnel 配置格式不符')
    return current


def configure(client, directory=RUNTIME_DIRECTORY, check_only=False):
    directory, runtime = local_runtime(directory)
    zone_id, records, baseline = preflight(client)
    prefix = f'/accounts/{ACCOUNT}'
    route_path = prefix + f'/cfd_tunnel/{TUNNEL_ID}/configurations'
    current = remote_config(client.get(route_path))
    config = expected_config()
    if current and current != config:
        raise SetupError('独立 Tunnel 已有不同路由或保护配置；不覆盖，请核对')
    if check_only:
        print('只读预检通过；未创建路由、DNS 或本地凭据。应用账号验证方案。')
        return
    if current != config:
        client.call('PUT', route_path, {'config': config})
    if remote_config(client.get(route_path)) != config:
        raise SetupError('Tunnel 应用入口路由未通过回读；不创建 DNS')
    dns_path = f'/zones/{zone_id}/dns_records'
    # Repeat the collision check immediately before the final publication step.
    actual_records = client.items(dns_path, name=HOSTNAME)
    if canonical(actual_records) != canonical(records):
        raise SetupError('配置期间目标 DNS 发生变化；停止，不覆盖')
    if not records:
        client.call('POST', dns_path, expected_dns())
    published = client.items(dns_path, name=HOSTNAME)
    if not matching_dns(published):
        raise SetupError('目标 DNS 回读不符；请在面板核对，助手不会自动删除已有配置')
    for name, before in baseline.items():
        if canonical(client.items(dns_path, name=name)) != before:
            raise SetupError('配置期间 根域主站 DNS 发生变化，请核对；助手没有修改它')
    token = client.get(prefix + f'/cfd_tunnel/{TUNNEL_ID}/token')
    save_runtime(directory, runtime, token)
    print('独立 Tunnel 应用入口路由和 DNS 均已配置并回读通过。')
    print('使用应用账号登录；根域和 www DNS 与配置前一致。')
    print('临时 API Token 未保存。本机仅保存此 Tunnel 的运行凭据。')
    print('下一步：关闭本窗口，打开 invitation-console.cmd，准备管理员，依次选择 1 和 7。')
    print('仍需真实 HTTPS 应用登录、角色权限、写入及拒绝访问验收，不能仅凭配置宣布上线完成。')
    return runtime


def main(argv=None):
    parser = argparse.ArgumentParser(description='准备独立应用入口；Windows 助手默认仅准备本机运行凭据')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--check-only', action='store_true', help='临时管理 API Token 的只读云端预检')
    modes.add_argument('--runtime-only', action='store_true',
                       help='仅准备本机 Tunnel 运行 Token；不要求管理 API Token，不验证云路由或 DNS')
    args = parser.parse_args(argv)
    try:
        validate_runtime()
        if not sys.stdin.isatty():
            raise SetupError('请双击 cloudflare-setup.cmd；不从命令参数、环境变量或重定向读取 Token')
        print(f'只配置 {HOSTNAME}；不会部署现有主站 Worker 或修改根域和 www DNS。')
        if args.runtime_only:
            print('仅准备本机凭据；请先在 Cloudflare 网页配置独立 Tunnel 的应用入口和 DNS。')
            print('请先关闭邀请控制台，可粘贴裸 Token 或此页复制的 cloudflared.exe service install Windows 命令（隐藏输入）。')
        else:
            print('请先关闭邀请控制台，再输入短期、受限的管理 API Token（不会显示或保存）。')
        with ConsoleLock(RUNTIME_DIRECTORY):
            prompt = '独立 Tunnel 运行 Token 或 Windows 命令：' if args.runtime_only else '临时 Cloudflare 管理 API Token：'
            entered = getpass.getpass(prompt)
            if args.runtime_only:
                # Only extract text; never invoke the copied service-install command.
                match = re.fullmatch(r'(?:cloudflared\.exe service install )?([A-Za-z0-9+/]+={0,2})', entered)
                if match is None:
                    raise SetupError('仅接受裸 Token 或固定 Windows 安装命令加单一 Token；不能含其他参数、换行或拼接')
                prepare_runtime(match.group(1))
            else:
                token = entered.strip()
                if not token or any(char.isspace() for char in token):
                    raise SetupError('Token 不能为空或含空白')
                client = Client(token)
                print('正在只读验证临时 Token...')
                verify_api_token(client)
                print('验证成功：Token 为 active；接下来核对账号范围和资源权限。')
                configure(client, check_only=args.check_only)
        return 0
    except (SetupError, ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f'配置未完成：{exc}', file=sys.stderr)
        print('助手不会自动删除或覆盖冲突；修正提示后可重开并核对当前状态。', file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print('\n配置已取消；请核对本机及面板中已完成的步骤。')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
