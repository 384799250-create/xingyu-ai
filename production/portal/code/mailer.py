"""SMTP configuration stays in /data; only delivery availability is public."""
import asyncio
from email.message import EmailMessage
import json
from pathlib import Path
import smtplib
import ssl


class MailUnavailable(Exception):
    pass


class EmailSender:
    def __init__(self, config_path=None):
        self.path = Path(config_path) if config_path else None
        self.slots = asyncio.Semaphore(2)

    def settings(self):
        try:
            if not self.path or self.path.is_symlink() or self.path.stat().st_mode & 0o077:
                return None
            values = json.loads(self.path.read_text(encoding='utf-8'))
            if values.get('mode') not in ('ssl', 'starttls'):
                return None
            if not isinstance(values.get('port'), int) or not 1 <= values['port'] <= 65535:
                return None
            for key in ('host', 'username', 'password', 'from'):
                if not isinstance(values.get(key), str) or not values[key] or '\r' in values[key] or '\n' in values[key]:
                    return None
            if '@' not in values['from']:
                return None
            return values
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    @property
    def configured(self):
        return self.settings() is not None

    async def send_code(self, recipient, code, purpose):
        config = self.settings()
        if config is None:
            raise MailUnavailable('not_configured')
        message = EmailMessage()
        message['From'], message['To'] = config['from'], recipient
        message['Subject'] = '星屿 · 邮箱验证码'
        action = {'verify':'验证找回密码邮箱','recover':'找回星屿网页账号的密码','register':'注册星屿邮箱账号'}[purpose]
        message.set_content(f'你好，\n\n你正在{action}。\n验证码：{code}\n\n验证码 10 分钟内有效，只能使用一次。请勿转发给他人。\n如非本人操作，请忽略此邮件。\n\n星屿\nhttps://www.example.com/ai/')

        def deliver():
            context = ssl.create_default_context()
            client_type = smtplib.SMTP_SSL if config['mode'] == 'ssl' else smtplib.SMTP
            options = {'timeout': 4}
            if config['mode'] == 'ssl':
                options['context'] = context
            with client_type(config['host'], config['port'], **options) as client:
                if config['mode'] == 'starttls':
                    client.ehlo(); client.starttls(context=context); client.ehlo()
                client.login(config['username'], config['password'])
                client.send_message(message)
        try:
            async with self.slots:
                await asyncio.to_thread(deliver)
        except Exception:
            # SMTP replies can contain addresses/credentials; never propagate them.
            raise MailUnavailable('delivery_failed') from None
