"""JSAPI order preparation; browser results never mutate the wallet."""
import re
from contextlib import closing
from dataclasses import replace
from alipay_errors import PaymentError
from wechat_oauth import digest
import star_wallet as wallet

async def call(service,corp,user,agent,action,payload):
    oauth=service.oauth;ops,ledger=service.scopes[corp]
    if not service.enabled(corp,user):raise PaymentError('微信支付暂未开放')
    common={'_session','_redirect'}
    if action=='authorize' and set(payload)==common:return oauth.begin(ledger,corp,user,agent,payload)
    if action=='authorized' and set(payload)==common|{'code','state'}:return await oauth.complete(ledger,corp,user,agent,payload)
    if action=='create_jsapi' and set(payload)==common|{'request_id','amount_fen'}:
        payer=oauth.payer(ledger,corp,user,agent,payload)
        config=replace(service.client.config,appid=oauth.config['appid']);config.ready()
        with service.allocation_lock(corp):
            service.check_member(corp,user,agent)
            with closing(wallet.connect(ops)) as db:
                db.execute('BEGIN IMMEDIATE');wallet.account(db,corp,user);db.commit()
            row,fresh=ledger.new_order(corp,user,payload['request_id'],payload['amount_fen'],config,trade_type='JSAPI',payer_hash=digest(payer))
        if fresh:
            ledger.update(row['id'],'creating')
            try:
                result=await service.client.jsapi(row,payer);prepay=result.get('prepay_id')
                if not isinstance(prepay,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',prepay):raise PaymentError('微信付款入口尚未确认')
                ledger.update(row['id'],'awaiting_payment',code_url=prepay)
            except PaymentError:ledger.update(row['id'],'query_required',error='下单结果待确认，请查询原订单，勿重复付款')
        return service.public(ledger,ledger.get(row['id']))
    if action=='pay' and set(payload)==common|{'id','page_url'}:
        payer=oauth.payer(ledger,corp,user,agent,payload);row=ledger.get(payload['id'],corp,user)
        if row['provider']!='wechat' or row['checkout_mode']!='JSAPI' or row['payer_hash']!=digest(payer) or row['appid']!=oauth.config['appid']:raise PaymentError('当前微信付款身份与原订单不匹配')
        if row['state']!='awaiting_payment' or service.public(ledger,row)['qr_expired']:raise PaymentError('本笔订单已付款或付款入口失效，请先查询原订单')
        return {'payment':service.client.bridge(row),'sdk':await oauth.sdk(payload)}
    raise PaymentError('微信内付款请求字段无效')
