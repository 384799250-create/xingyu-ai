"""Run on the server's private terminal. Never prints the application Secret."""
import argparse
import getpass
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / 'app.env'
CONTAINER = 'xiaoyi-app-1000006'
AGENT_ID = 1000006


def read_env(path=ENV_FILE):
    values = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        if line and not line.startswith('#'):
            key, value = line.split('=', 1)
            values[key] = value
    return values


def write_env(values, path=ENV_FILE):
    for key, value in values.items():
        if not re.fullmatch('[A-Z_][A-Z0-9_]*', key) or any(c in value for c in '\r\n\x00'):
            raise ValueError('Invalid environment field')
    temp = path.with_name(path.name + '.new')
    with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w', encoding='utf-8') as handle:
        handle.write(''.join(key + '=' + values[key] + '\n' for key in sorted(values)))
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def docker(args, check=True):
    result = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=75)
    if check and result.returncode:
        raise RuntimeError('Docker operation failed; inspect the pilot container locally')
    return result


def validate_credentials(values, secret, opener=urllib.request.urlopen):
    query = urllib.parse.urlencode({'corpid': values['WECOM_CORP_ID'], 'corpsecret': secret})
    try:
        with opener('https://qyapi.weixin.qq.com/cgi-bin/gettoken?' + query, timeout=15) as response:
            data = json.load(response)
        if data.get('errcode', 0) or not data.get('access_token'):
            raise ValueError('WeCom credential validation failed (code %s). Check Secret and trusted IP.' % data.get('errcode', 'unknown'))
        query = urllib.parse.urlencode({'access_token': data['access_token'], 'agentid': AGENT_ID})
        with opener('https://qyapi.weixin.qq.com/cgi-bin/agent/get?' + query, timeout=15) as response:
            app = json.load(response)
        if app.get('errcode', 0) or app.get('agentid') != AGENT_ID:
            raise ValueError('Credentials do not grant access to application 1000006')
    except ValueError:
        raise
    except Exception:
        # urllib exception strings can contain credential-bearing URLs.
        raise RuntimeError('Cannot validate with WeCom. Check server network and trusted IP.') from None


def show_callback(values):
    print('\n以下三项只填写到企业微信“设置 API 接收”页面，不要发到聊天：')
    print('URL: https://www.example.com/wecom/apps/1000006')
    print('Token: ' + values['CALLBACK_TOKEN'])
    print('EncodingAESKey: ' + values['CALLBACK_AES_KEY'])
    print('新应用企业可信 IP: 192.0.2.10')
    print('AgentId: 1000006')
    print('当前 AI 回复: ' + ('已启用' if values.get('ENABLE_AI') == 'true' else '未启用（仅回调验证）'))
    print('应用 Secret 从不显示。')


def healthy(enabled):
    code = "import urllib.request,json;d=json.load(urllib.request.urlopen('http://127.0.0.1:8088/health',timeout=3));print(json.dumps(d['independent_app']))"
    for _ in range(18):
        result = docker(['exec', CONTAINER, 'python', '-c', code], check=False)
        if result.returncode == 0:
            state = json.loads(result.stdout)
            if state.get('agent_id') == AGENT_ID and state.get('ai_enabled') == enabled:
                return True
        time.sleep(1)
    return False


def enable(values, replace_secret=False, enabled=True):
    secret = values.get('WECOM_APP_SECRET', '')
    if replace_secret or not secret:
        secret = getpass.getpass('粘贴新应用 1000006 的 Secret（输入不显示，按回车确认）: ').strip()
    if not secret or any(c in secret for c in '\r\n\x00'):
        raise ValueError('A valid application Secret is required')
    validate_credentials(values, secret)
    current = json.loads(docker(['inspect', CONTAINER]).stdout)[0]
    if current['Config'].get('Labels', {}).get('xiaoyi.pilot.agent') != str(AGENT_ID):
        raise RuntimeError('Container identity mismatch; no changes applied')
    run_args = json.loads((ROOT / 'container-run.json').read_text())
    previous = dict(values)
    values = dict(values, WECOM_APP_SECRET=secret, ENABLE_AI=str(enabled).lower())
    write_env(values)
    try:
        docker(['stop', '-t', '20', CONTAINER])
        docker(['rm', CONTAINER])
        docker(run_args)
        if not healthy(enabled):
            raise RuntimeError('Pilot failed its startup check')
    except BaseException:
        write_env(previous)
        found = docker(['inspect', CONTAINER], check=False)
        if found.returncode == 0:
            docker(['rm', '-f', CONTAINER], check=False)
        restarted = docker(run_args, check=False).returncode == 0
        restored = restarted and healthy(previous.get('ENABLE_AI') == 'true')
        if restored:
            raise RuntimeError('Enable failed; previous pilot configuration has been restored') from None
        raise RuntimeError('Enable failed; previous configuration saved, but pilot restart needs inspection') from None
    print('新应用凭据已验证。AI状态：' + ('已启用' if enabled else '未启用，等待应用池分配'))
    return values


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--enable', action='store_true')
    group.add_argument('--configure-only', action='store_true')
    parser.add_argument('--replace-secret', action='store_true')
    args = parser.parse_args()
    if not hasattr(os, 'geteuid') or os.geteuid() != 0:
        raise SystemExit('请在服务器使用 sudo python3 运行此文件。')
    values = read_env()
    if values.get('WECOM_AGENT_ID') != str(AGENT_ID):
        raise SystemExit('应用配置身份不匹配，未执行操作。')
    if args.enable or args.configure_only:
        values = enable(values, args.replace_secret, enabled=args.enable)
    show_callback(values)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
