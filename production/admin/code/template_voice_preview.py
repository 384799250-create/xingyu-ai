"""Admin-only fixed TTS samples. No persona writes or end-user wallet charges."""
import base64
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing, contextmanager

from persona_templates import TemplateError

MAX_AUDIO = 1024 * 1024
TEXTS = {'zh': '你好，我是星屿。', 'en': 'Hello, I am Xingyu.', 'ja': 'こんにちは、星屿です。'}
INSTRUCTIONS = {
    'zh': '自然平静地说话。保持原有音色，只朗读正文，不朗读这些指令，不增加台词、笑声、哭声或音效。',
    'en': 'Speak naturally and calmly. Keep the original voice. Read only the text, never these instructions. Do not add dialogue, laughter, crying or sound effects.',
    'ja': '自然で落ち着いた口調で話す。元の声質を保つ。本文だけを読み、これらの指示は読まない。台詞、笑い声、泣き声、効果音を追加しない。',
}


def parameters(data):
    if not isinstance(data, dict) or set(data) != {'provider', 'voice', 'speed', 'language'}:
        raise TemplateError('试听参数无效，请重新选择音色。')
    provider, voice, speed, language = (data[k] for k in ('provider', 'voice', 'speed', 'language'))
    if not isinstance(provider, str) or provider not in ('breeze', 'tencent', 'openai'):
        raise TemplateError('请选择语音服务。')
    pattern = {'breeze': r'voc_[A-Za-z0-9_-]{1,96}', 'tencent': r'[1-9][0-9]{0,8}', 'openai': r'[A-Za-z0-9_-]{1,100}'}[provider]
    if not isinstance(voice, str) or not re.fullmatch(pattern, voice):
        raise TemplateError('请填写有效的音色编号后试听。')
    if type(speed) not in (int, float) or not math.isfinite(speed) or not .5 <= speed <= 2:
        raise TemplateError('试听语速应在 0.5 至 2 倍之间。')
    if not isinstance(language, str) or language not in TEXTS:
        raise TemplateError('请选择有效的语音语言。')
    if language != 'zh' and provider != 'breeze':
        raise TemplateError('当前仅 Breeze 支持外语试听，请选择中文。')
    return provider, voice, float(speed), language


def configuration(ops, provider):
    # Use effective trusted server configuration; never accept endpoints or keys from clients.
    info = ops.info(1000003)
    env = dict(v.split('=', 1) for v in info['Config']['Env'] if '=' in v)
    if env.get('XIAOYI_TTS_PROVIDER', '').strip().lower() != provider:
        raise TemplateError('该语音服务尚未配置，请选择当前已配置的服务后试听。')
    if provider == 'breeze':
        url = env.get('XIAOYI_BREEZE_TTS_URL', 'https://api.breeze.blue/v1').rstrip('/')
        model = env.get('XIAOYI_BREEZE_TTS_MODEL', 'breeze-tts-2-multilingual')
        key = env.get('XIAOYI_BREEZE_TTS_KEY') or env.get('BREEZE_API_KEY')
        config = {'url': url, 'model': model, 'key': key}
    elif provider == 'openai':
        config = {'url': env.get('XIAOYI_TTS_URL', ''), 'model': env.get('XIAOYI_TTS_MODEL', ''), 'key': env.get('XIAOYI_TTS_KEY', '')}
    else:
        config = {'url': 'https://tts.tencentcloudapi.com', 'model': '',
                  'id': env.get('XIAOYI_TENCENT_TTS_SECRET_ID') or env.get('XIAOYI_TENCENT_ASR_SECRET_ID'),
                  'key': env.get('XIAOYI_TENCENT_TTS_SECRET_KEY') or env.get('XIAOYI_TENCENT_ASR_SECRET_KEY')}
    url = urllib.parse.urlsplit(config['url'])
    if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise TemplateError('语音服务地址配置无效，请检查后台配置。')
    if not config['key'] or (provider == 'tencent' and not config['id']):
        raise TemplateError('语音服务密钥尚未配置。')
    return config


def cache_key(provider, config, voice, speed, language):
    identity = (3, (provider, config['url'], config['model']), TEXTS[language], voice, float(speed), 'neutral')
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()


def valid_mp3(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_AUDIO:
        return False
    offset, frames, stream_format = 0, 0, None
    if raw.startswith(b'ID3'):
        if len(raw) < 10 or any(v & 128 for v in raw[6:10]):
            return False
        offset = 10 + sum(raw[6+i] << (21-7*i) for i in range(4)) + (10 if raw[5] & 16 else 0)
    while offset < len(raw):
        if raw[offset:offset+3] == b'TAG' and len(raw)-offset == 128:
            break
        if offset + 4 > len(raw):
            return False
        header = int.from_bytes(raw[offset:offset+4], 'big')
        version, layer = (header >> 19) & 3, (header >> 17) & 3
        bitrate_id, sample_id = (header >> 12) & 15, (header >> 10) & 3
        if header >> 21 != 2047 or version == 1 or layer != 1 or bitrate_id in (0, 15) or sample_id == 3:
            return False
        rates = ((0,8,16,24,32,40,48,56,64,80,96,112,128,144,160,0) if version != 3 else
                 (0,32,40,48,56,64,80,96,112,128,160,192,224,256,320,0))
        sample_rate = (44100,48000,32000)[sample_id] // (1 if version == 3 else 2 if version == 2 else 4)
        length = (144 if version == 3 else 72) * rates[bitrate_id] * 1000 // sample_rate + ((header >> 9) & 1)
        current = (version, sample_rate, (header >> 6) & 3)
        if length < 4 or offset + length > len(raw) or (stream_format is not None and stream_format != current):
            return False
        stream_format = current
        frames += 1
        offset += length
    return frames > 0


def read_cache(directory, key):
    path = directory / (key + '.mp3')
    try:
        if path.is_symlink() or path.stat().st_size > MAX_AUDIO:
            return None
        raw = path.read_bytes()
        return raw if valid_mp3(raw) else None
    except FileNotFoundError:
        return None


@contextmanager
def generation_lock(directory):
    import fcntl
    path = directory / '.generation.lock'
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as stream:
        st = directory.stat()
        os.fchown(stream.fileno(), st.st_uid, st.st_gid)
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TemplateError('其他试听正在准备中，请稍后再试。') from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def save_cache(directory, key, raw):
    path = directory / (key + '.mp3')
    tmp = directory / ('.admin-preview-' + secrets.token_hex(8))
    try:
        with os.fdopen(os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'wb') as stream:
            stream.write(raw);stream.flush();os.fsync(stream.fileno())
            st = directory.stat();os.fchown(stream.fileno(), st.st_uid, st.st_gid)
        os.replace(tmp, path)
        files = sorted(directory.glob('*.mp3'), key=lambda p:p.lstat().st_mtime, reverse=True)
        for old in files[256:]:
            if old != path:
                old.unlink(missing_ok=True)
    finally:
        tmp.unlink(missing_ok=True)


def reserve(ops, actor, key, provider):
    # Administrative provider usage only. This database never touches user wallets.
    database = ops.directory / 'template-voice-preview.sqlite3'
    with closing(sqlite3.connect(database, timeout=3)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY, actor TEXT, sample TEXT, provider TEXT, created REAL, status TEXT)')
        db.execute('BEGIN IMMEDIATE')
        now = time.time();operator = str(actor['username'])
        db.execute('DELETE FROM attempts WHERE created < ?', (now-86400*30,))
        if db.execute('SELECT count(*) FROM attempts WHERE created > ?', (now-86400,)).fetchone()[0] >= 100 or db.execute('SELECT count(*) FROM attempts WHERE actor=? AND created>?', (operator, now-86400)).fetchone()[0] >= 40:
            raise TemplateError('今天的后台试听生成次数已用完，已有试听仍可播放。')
        if db.execute('SELECT count(*) FROM attempts WHERE created > ?', (now-60,)).fetchone()[0] >= 12:
            raise TemplateError('试听生成过于频繁，请稍后再试。')
        ident = secrets.token_hex(16)
        db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?)', (ident, operator, key, provider, now, 'requested'))
    database.chmod(0o600)
    return ident


def finish(ops, ident, status):
    with closing(sqlite3.connect(ops.directory / 'template-voice-preview.sqlite3', timeout=3)) as db, db:
        db.execute('UPDATE attempts SET status=? WHERE id=?', (status, ident))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch(url, headers, body):
    # Breeze's edge protection rejects urllib's default User-Agent (403 / 1010).
    # Identify this integration explicitly, keeping the existing authenticated HTTPS API.
    headers = dict(headers, **{'User-Agent': 'Xingyu-Admin/1.0'})
    request = urllib.request.Request(url, data=body, headers=headers, method='POST')
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=18) as response:
            raw = response.read(MAX_AUDIO*2+1)
            if len(raw) > MAX_AUDIO*2:
                raise TemplateError('试听音频过大，请更换音色后重试。')
            return raw
    except urllib.error.HTTPError as exc:
        # Access protection and voice entitlement failures are not proof of a bad key.
        if exc.code == 401:
            message = '语音服务鉴权失败，请检查服务配置。'
        elif exc.code == 403:
            message = '语音服务拒绝访问，请检查服务连接或该音色的使用权限。'
        elif exc.code == 429:
            message = '语音服务请求过于频繁，请稍后再试。'
        else:
            message = '语音服务暂时无法生成此音色，请稍后重试。'
        exc.close()
        raise TemplateError(message) from None
    except (OSError, TimeoutError):
        raise TemplateError('试听生成超时或连接中断，请稍后再试。') from None


def tencent_speed(speed):
    value = max(.6, min(2.5, speed))
    points = ((.6,-2),(.8,-1),(1,0),(1.2,1),(1.5,2),(2.5,6))
    for (a,x),(b,y) in zip(points,points[1:]):
        if value <= b:
            return round(x+(value-a)*(y-x)/(b-a),2)
    return 6


def synthesize(provider, config, voice, speed, language):
    text = TEXTS[language]
    if provider == 'breeze':
        url = config['url']+'/text-to-speech/'+urllib.parse.quote(voice, safe='')+'?output_format=mp3'
        headers = {'xi-api-key':config['key'], 'Content-Type':'application/json'}
        body = {'text':text, 'model_id':config['model'], 'language_code':language,
                'voice_settings':{'speed':speed,'guidance_scale':2.5,'volume':1}, 'instructions':INSTRUCTIONS[language]}
    elif provider == 'openai':
        url = config['url'];headers = {'Authorization':'Bearer '+config['key'],'Content-Type':'application/json'}
        body = {'model':config['model'],'input':text,'voice':voice,'speed':speed,'response_format':'mp3'}
    else:
        body = {'Text':text,'SessionId':secrets.token_hex(16),'ModelType':1,'VoiceType':int(voice),'Codec':'mp3','SampleRate':16000,'Speed':tencent_speed(speed)}
        payload = json.dumps(body,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
        timestamp = str(int(time.time()));date = time.strftime('%Y-%m-%d',time.gmtime(int(timestamp)))
        digest = lambda b:hashlib.sha256(b).hexdigest()
        sign = lambda key,b:hmac.new(key,b.encode(),hashlib.sha256).digest()
        canonical = 'POST\n/\n\ncontent-type:application/json; charset=utf-8\nhost:tts.tencentcloudapi.com\n\ncontent-type;host\n'+digest(payload)
        scope = date+'/tts/tc3_request'
        signing = sign(sign(sign(('TC3'+config['key']).encode(),date),'tts'),'tc3_request')
        signature = hmac.new(signing,('TC3-HMAC-SHA256\n'+timestamp+'\n'+scope+'\n'+digest(canonical.encode())).encode(),hashlib.sha256).hexdigest()
        headers = {'Content-Type':'application/json; charset=utf-8','Host':'tts.tencentcloudapi.com',
                   'X-TC-Action':'TextToVoice','X-TC-Version':'2019-08-23','X-TC-Timestamp':timestamp,
                   'Authorization':'TC3-HMAC-SHA256 Credential='+config['id']+'/'+scope+', SignedHeaders=content-type;host, Signature='+signature}
        raw = fetch(config['url']+'/',headers,payload)
        try:
            result = json.loads(raw)['Response']
            if result.get('Error'):
                raise ValueError('provider_rejected')
            return base64.b64decode(result['Audio'],validate=True)
        except (ValueError,KeyError,TypeError):
            raise TemplateError('腾讯云未能生成此音色，请检查音色权限和语音额度。') from None
    return fetch(url,headers,json.dumps(body,ensure_ascii=False).encode())


def preview(ops, actor, data):
    if actor.get('role') != 'superadmin':
        raise TemplateError('只有超级管理员可试听官方模板音色。')
    provider,voice,speed,language = parameters(data)
    try:
        config = configuration(ops,provider)
        directory = ops.root / 'voice-preview-cache'
        if directory.is_symlink():
            raise TemplateError('试听缓存配置无效。')
        directory.mkdir(mode=0o700,exist_ok=True)
        key = cache_key(provider,config,voice,speed,language)
        audio = read_cache(directory,key);cached = audio is not None
        if audio is None:
            with generation_lock(directory):
                audio = read_cache(directory,key);cached = audio is not None
                if audio is None:
                    ident = reserve(ops,actor,key,provider)
                    try:
                        audio = synthesize(provider,config,voice,speed,language)
                        if not valid_mp3(audio):
                            raise TemplateError('语音服务未返回可播放的音频，请稍后重试。')
                        save_cache(directory,key,audio)
                    except Exception:
                        finish(ops,ident,'failed');raise
                    finish(ops,ident,'ready')
        return {'audio':base64.b64encode(audio).decode(),'mime':'audio/mpeg','cached':cached,'text':TEXTS[language]}
    except TemplateError:
        raise
    except Exception:
        # Provider/OS exceptions may contain credentials or deployment paths.
        raise TemplateError('试听服务暂不可用，请稍后重试。') from None
