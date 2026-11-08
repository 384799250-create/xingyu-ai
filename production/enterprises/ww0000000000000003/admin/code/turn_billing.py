"""Read-only bill presentation. No wallet, pricing or settlement mutations."""
import json,re
import shadow_pricing as pricing
PAGE=20
KEY="CASE WHEN COALESCE(json_extract(snapshot,'$.billing_turn'),'')!='' THEN 'turn:'||json_extract(snapshot,'$.billing_turn') ELSE 'request:'||kind||':'||request_id END"
# Legacy events have no batch ID. Infer only sticker-only sessions separated by
# more than ten minutes, inside one wallet and application. Never rewrite events.
BASE="""WITH owned AS (SELECT * FROM events WHERE wallet=?),
legacy_previous AS (
 SELECT id,agent,created,LAG(created) OVER(PARTITION BY agent ORDER BY created,id) AS previous
 FROM owned WHERE kind='token' AND json_extract(snapshot,'$.purpose')='sticker_analysis'
 AND COALESCE(json_extract(snapshot,'$.billing_turn'),'')=''),
legacy_sessions AS (
 SELECT *,SUM(CASE WHEN previous IS NULL OR created-previous>600 THEN 1 ELSE 0 END)
 OVER(PARTITION BY agent ORDER BY created,id ROWS UNBOUNDED PRECEDING) AS session FROM legacy_previous),
legacy_keys AS (
 SELECT id,'sticker-legacy:'||MIN(id) OVER(PARTITION BY agent,session) AS batch_key FROM legacy_sessions),
scoped AS (SELECT owned.*,COALESCE(legacy_keys.batch_key,"""+KEY+""") AS group_key
 FROM owned LEFT JOIN legacy_keys ON legacy_keys.id=owned.id) """
FIELDS="""MIN(id) AS anchor,agent,group_key,MIN(created) AS created,MAX(created) AS latest,
COUNT(*) AS item_count,SUM(amount IS NULL) AS unknown_count,SUM(COALESCE(amount,0)) AS amount,
SUM(review!=0) AS review_count,SUM(state='insufficient') AS insufficient_count,
SUM(state='queued') AS queued_count,SUM(state='paid') AS paid_count,SUM(state='reversed') AS reversed_count,
SUM(kind='token') AS token_calls,
SUM(CASE WHEN kind='image' AND json_extract(snapshot,'$.status')='completed' THEN COALESCE(json_extract(snapshot,'$.image_count'),0) ELSE 0 END) AS image_count,
SUM(kind='tts' AND json_extract(snapshot,'$.status')='completed' AND COALESCE(json_extract(snapshot,'$.generated_chars'),0)>0) AS voice_count,
SUM(kind='image') AS image_attempts,SUM(kind='tts') AS voice_attempts,
SUM(kind='image' AND json_extract(snapshot,'$.status') IN ('pending','unknown')) AS image_pending,
SUM(kind='tts' AND json_extract(snapshot,'$.status') IN ('pending','unknown')) AS voice_pending"""

def summary(row,receipt=None):
    value=dict(row);anchor=value.pop('anchor');value['id']=str(anchor)
    raw_key=value.pop('group_key');turn=raw_key[5:] if raw_key.startswith('turn:') else ''
    value['scene']='sticker_batch' if raw_key.startswith(('sticker-legacy:','turn:sticker_batch:')) else turn.split(':',1)[0] if turn else 'standalone'
    value['batch_inferred']=raw_key.startswith('sticker-legacy:')
    value['amount']=None if value['unknown_count']==value['item_count'] else pricing.money(value['amount'])
    value['has_image']=value['image_count']>0;value['has_voice']=value['voice_count']>0
    turn_state=receipt(value['agent'],turn) if turn and receipt and value['scene']!='sticker_batch' else None
    value['turn_state']=turn_state or 'unknown'
    value['state']='review' if value['review_count'] else 'insufficient' if value['insufficient_count'] else 'processing' if turn_state in ('working','pending') else 'waiting' if value['unknown_count'] else 'queued' if value['queued_count'] else 'reversed' if value['reversed_count'] else 'paid' if value['paid_count'] else 'excluded'
    return value

def overview(db,ident,page=1,receipt=None):
    total=db.execute(BASE+'SELECT COUNT(*) FROM (SELECT 1 FROM scoped GROUP BY agent,group_key)',(ident,)).fetchone()[0]
    rows=db.execute(BASE+'SELECT '+FIELDS+' FROM scoped GROUP BY agent,group_key ORDER BY latest DESC,anchor DESC LIMIT ? OFFSET ?',(ident,PAGE,(page-1)*PAGE)).fetchall()
    return {'bill_groups':[summary(r,receipt) for r in rows],'bill_count':total,'bill_page_size':PAGE,'bill_view':'turns-v1'}

def detail(db,ident,group,page=1,receipt=None):
    if not isinstance(group,str) or not re.fullmatch('[1-9][0-9]{0,17}',group):raise ValueError('Invalid bill group')
    anchor=db.execute(BASE+'SELECT agent,group_key FROM scoped WHERE id=?',(ident,int(group))).fetchone()
    if anchor is None:raise ValueError('Bill group unavailable')
    args=(ident,anchor['agent'],anchor['group_key'])
    row=db.execute(BASE+'SELECT '+FIELDS+' FROM scoped WHERE agent=? AND group_key=? GROUP BY agent,group_key',args).fetchone()
    if str(row['anchor'])!=group:raise ValueError('Invalid group anchor')
    rows=db.execute(BASE+'SELECT id,kind,amount,state,reason,created,snapshot,review FROM scoped WHERE agent=? AND group_key=? ORDER BY created,id LIMIT ? OFFSET ?',args+(PAGE,(page-1)*PAGE)).fetchall()
    items=[]
    for raw in rows:
        item=dict(raw);snap=json.loads(item.pop('snapshot'))
        item['amount']=pricing.money(item['amount']);item['agent']=row['agent']
        item.update({k:snap.get(k) for k in ('purpose','input_tokens','output_tokens','cached_tokens','image_count','generated_chars','status','billing_version')})
        item['is_retry']=bool(snap.get('is_retry'));items.append(item)
    return {'bill_group':summary(row,receipt),'items':items,'page':page,'page_size':PAGE,'count':row['item_count'],'bill_view':'turns-v1'}
