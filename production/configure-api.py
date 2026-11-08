#!/usr/bin/env python3
"""Optional provider configuration; secrets are entered on the server, never in the web UI."""
import datetime
import getpass
import importlib.util
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit


def env_value(value):
    # Compose dotenv single quotes preserve $ and # literally. Reject quotes/newlines explicitly.
    if any(c in value for c in "'\r\n\x00"):
        raise ValueError('输入不能包含单引号或换行')
    return "'" + value + "'"


def replace_env(text, updates):
    for key, value in updates.items():
        line = key + '=' + env_value(value)
        expression = r'(?m)^\s*' + re.escape(key) + r'\s*=.*$'
        if re.search(expression, text):
            text = re.sub(expression, lambda match: line, text)
        else:
            text = text.rstrip('\n') + '\n' + line + '\n'
    return text


def load_installer():
    path = Path(__file__).resolve().with_name('install.py')
    spec = importlib.util.spec_from_file_location('suite_install', path)
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    return installer


def main():
    if sys.platform != 'linux' or os.geteuid() != 0:
        raise ValueError('请在服务器执行 sudo python3 configure-api.py')
    os.umask(0o077)
    installer = load_installer()
    project = installer.PROJECT
    envpath = project / '.env'
    if project.is_symlink() or envpath.is_symlink() or not envpath.is_file():
        raise ValueError('服务器 .env 不存在或为链接，未修改')
    compose = ['docker', 'compose', '--project-directory', str(project), '-f', str(project / 'compose.yaml')]
    current = installer.health(compose)
    if current.get('suite_version') != 2:
        raise ValueError('请先安装完整扩展版')
    print('1 配置 OpenAI 兼容语音合成；2 配置生图；3 停用语音合成、生图和语音识别；4 配置 OpenAI 兼容语音识别；5 配置腾讯云短音频识别；6 配置腾讯云语音合成；回车退出。密钥仅保存在服务器 .env。')
    choice = input('选择：').strip()
    if not choice:
        return
    if choice == '3':
        updates = {'XIAOYI_TTS_KEY': '', 'XIAOYI_IMAGE_KEY': '', 'XIAOYI_STT_KEY': '',
                   'XIAOYI_TTS_PROVIDER': '', 'XIAOYI_TENCENT_TTS_SECRET_ID': '', 'XIAOYI_TENCENT_TTS_SECRET_KEY': '',
                   'XIAOYI_TENCENT_ASR_SECRET_ID': '', 'XIAOYI_TENCENT_ASR_SECRET_KEY': ''}
    elif choice == '6':
        print('腾讯云 TextToVoice：需单独开通语音合成。AMR 以微信语音气泡发送，服务器需有 AMR 编码组件。')
        print('SecretId、SecretKey 均留空时，复用服务器已有腾讯云识别密钥，需具有 TTS 调用权限。')
        secret_id = input('语音合成 SecretId（可留空）：').strip()
        secret_key = getpass.getpass('语音合成 SecretKey（可留空，输入不显示）：').strip()
        if bool(secret_id) != bool(secret_key):
            raise ValueError('SecretId、SecretKey 必须一起填写或一起留空')
        if secret_id and (not re.fullmatch(r'[\x21-\x7e]{1,128}', secret_id) or not re.fullmatch(r'[\x21-\x7e]{1,256}', secret_key)):
            raise ValueError('SecretId 或 SecretKey 格式无效')
        print('自然多情感音色：601009 爱小芊、601010 爱小娇、601008 爱小豪；402001 云小团仅中性。')
        voice = input('默认腾讯云数字音色编号 [601009]：').strip() or '601009'
        if not re.fullmatch(r'[1-9][0-9]{0,8}', voice):
            raise ValueError('音色编号需为腾讯云提供的数字 ID')
        voices = input('允许用户选择的音色编号，逗号分隔（留空不限制）：').strip()
        if any(not re.fullmatch(r'[1-9][0-9]{0,8}', value.strip()) for value in voices.split(',') if value.strip()):
            raise ValueError('允许的音色编号需为数字 ID')
        updates = {'XIAOYI_TTS_PROVIDER': 'tencent', 'XIAOYI_TTS_VOICE': voice,
                   'XIAOYI_TTS_VOICES': voices, 'XIAOYI_TTS_FORMAT': 'amr',
                   'XIAOYI_TENCENT_TTS_SECRET_ID': secret_id, 'XIAOYI_TENCENT_TTS_SECRET_KEY': secret_key}
    elif choice == '4':
        print('填写 OpenAI 兼容的语音转文字接口，例如 https://你的服务域名/v1/audio/transcriptions。')
        url = input('HTTPS 完整接口地址：').strip()
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment or parsed.query:
            raise ValueError('接口需为不带凭证和查询参数的 HTTPS 地址')
        model = input('语音识别模型编号：').strip()
        key = getpass.getpass('API Key（输入不显示）：').strip()
        if not model or not key:
            raise ValueError('模型和密钥不能为空')
        updates = {'XIAOYI_STT_URL': url, 'XIAOYI_STT_KEY': key, 'XIAOYI_STT_MODEL': model}
    elif choice == '5':
        print('配置腾讯云短音频识别（SentenceRecognition）。需要腾讯云 SecretId、SecretKey；不会显示或写入日志。')
        secret_id = input('腾讯云 SecretId：').strip()
        secret_key = getpass.getpass('腾讯云 SecretKey（输入不显示）：').strip()
        engine = input('识别引擎 [16k_zh]：').strip() or '16k_zh'
        if not secret_id or not secret_key or not re.fullmatch(r'[\x21-\x7e]{1,128}', secret_id) or not re.fullmatch(r'[\x21-\x7e]{1,256}', secret_key):
            raise ValueError('SecretId 或 SecretKey 格式无效')
        if not re.fullmatch(r'[\x21-\x7e]{1,64}', engine):
            raise ValueError('识别引擎格式无效')
        updates = {'XIAOYI_TENCENT_ASR_SECRET_ID': secret_id,
                   'XIAOYI_TENCENT_ASR_SECRET_KEY': secret_key,
                   'XIAOYI_TENCENT_ASR_ENGINE': engine}
    elif choice in ('1', '2'):
        prefix = 'TTS' if choice == '1' else 'IMAGE'
        print('填写完整兼容接口地址，例如 https://你的服务域名/v1/audio/speech 或 /v1/images/generations。')
        url = input('HTTPS 完整接口地址：').strip()
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment or parsed.query:
            raise ValueError('接口需为不带凭证和查询参数的 HTTPS 地址')
        if prefix == 'IMAGE' and parsed.path.rstrip('/') in ('', '/v1'):
            url = parsed._replace(path='/v1/images/generations').geturl()
            print('已补全生图接口路径：' + url)
        model = input('服务方提供的模型编号：').strip()
        key = getpass.getpass('API Key（输入不显示）：').strip()
        if not model or not key:
            raise ValueError('模型和密钥不能为空')
        updates = {'XIAOYI_' + prefix + '_URL': url, 'XIAOYI_' + prefix + '_KEY': key, 'XIAOYI_' + prefix + '_MODEL': model}
        if prefix == 'IMAGE':
            resolution = input('默认清晰度 1K/2K/4K [1K]：').strip().upper() or '1K'
            if resolution not in ('1K', '2K', '4K'):
                raise ValueError('清晰度只能是 1K、2K 或 4K')
            updates['XIAOYI_IMAGE_RESOLUTION'] = resolution
        if prefix == 'TTS':
            voice = input('默认音色编号 [alloy]：').strip() or 'alloy'
            fmt = input('返回格式 mp3/amr [mp3]：').strip() or 'mp3'
            if fmt not in ('mp3', 'amr'):
                raise ValueError('格式需为 mp3 或 amr')
            voices = input('允许用户选择的音色编号，逗号分隔（留空不限制）：').strip()
            updates.update(XIAOYI_TTS_PROVIDER='openai', XIAOYI_TTS_VOICE=voice, XIAOYI_TTS_FORMAT=fmt, XIAOYI_TTS_VOICES=voices)
    else:
        raise ValueError('未识别的选择，未修改')
    original = envpath.read_bytes()
    updated = replace_env(original.decode('utf-8-sig'), updates).encode()
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    backup = project / ('backup-api-' + stamp + '.env')
    installer.write(backup, original, 0o600)
    mode = envpath.stat().st_mode & 0o777
    try:
        installer.write(envpath, updated, 0o600)
        installer.run(compose + ['config', '--quiet'])
        installer.start(compose)
        installer.ready(compose, current['agent_id'])
    except BaseException:
        installer.write(envpath, original, mode)
        try:
            installer.start(compose)
            installer.ready(compose, current['agent_id'])
            print('原配置已恢复。')
        except Exception:
            print('恢复检查未完成，请保留备份：' + str(backup))
        raise
    print('配置已保存，密钥未显示。重新发送 我要设置，在语音与生图页进行一次微信实发验证。')
    print('旧 .env 备份也包含密钥，请仅保留在服务器：' + str(backup))


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('已中止。', file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print('配置未完成：' + (str(exc) if isinstance(exc, ValueError) else type(exc).__name__), file=sys.stderr)
        sys.exit(1)
