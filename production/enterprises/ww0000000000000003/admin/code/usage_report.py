"""Read-only aggregation of isolated application token ledgers."""
from contextlib import closing
from datetime import datetime,timedelta,timezone
import sqlite3
from pathlib import Path

ZONE=timezone(timedelta(hours=8))
FIELDS=('input_tokens','output_tokens','total_tokens','cached_tokens','cache_miss_tokens','reasoning_tokens')
GROUPS={'app':None,'user':'user','day':"date(created,'unixepoch','+8 hours')",'model':'model','purpose':'purpose'}

def report(ops, query):
    from admin_ops import Rejected,connect
    if not isinstance(query,dict) or set(query)-{'start','end','agent','user','group','page','resource'}:raise Rejected('用量查询条件无效。')
    resource=query.get('resource','token')
    if resource not in ('token','image','tts'):raise Rejected('用量类型无效。')
    group=query.get('group','app')
    if group not in GROUPS:raise Rejected('统计维度无效。')
    today=datetime.now(ZONE).date()
    try:
        start=datetime.strptime(query.get('start',(today-timedelta(days=29)).isoformat()),'%Y-%m-%d').replace(tzinfo=ZONE)
        end=datetime.strptime(query.get('end',today.isoformat()),'%Y-%m-%d').replace(tzinfo=ZONE)+timedelta(days=1)
        page=int(query.get('page',1))
        if not 1<=page<=10000 or not start<end or end-start>timedelta(days=3660):raise ValueError()
        agent=int(query['agent']) if query.get('agent') else None
        if agent is not None:ops.resolve(agent)
    except (ValueError,TypeError):raise Rejected('请选择有效日期（最多十年）和页码。') from None
    user=query.get('user','')
    if not isinstance(user,str) or len(user)>200:raise Rejected('用户标识无效。')
    known=['reported_input','reported_output','reported_total','reported_cached','reported_miss','reported_reasoning']
    media_fields=('successful_requests','unknown_requests','retry_requests','submitted_chars','generated_chars','image_count','preview_requests','preview_chars')
    def blank():return dict(requests=0,missing_requests=0,failed_requests=0,pending_requests=0,**{f:0 for f in (*FIELDS,*known,*media_fields)})
    summary=blank();items=[];unavailable=[];unattributed=0;first_recorded=None;ledger_apps=[]
    select="COUNT(*) AS requests,SUM(source!='reported') AS missing_requests,SUM(status IN ('error','cancelled','interrupted')) AS failed_requests,SUM(status='pending') AS pending_requests,"+','.join('SUM('+f+') AS '+f for f in FIELDS)+','+','.join('COUNT('+f+') AS '+k for f,k in zip(FIELDS,known))
    table='token_requests'
    if resource!='token':
        table='media_requests'
        select="COUNT(*) AS requests,SUM(status='completed') AS successful_requests,SUM(status='error') AS failed_requests,SUM(status IN ('unknown','cancelled','interrupted')) AS unknown_requests,SUM(status='pending') AS pending_requests,SUM(is_retry) AS retry_requests,SUM(submitted_chars) AS submitted_chars,SUM(generated_chars) AS generated_chars,SUM(image_count) AS image_count,SUM(purpose='preview') AS preview_requests,SUM(CASE WHEN purpose='preview' THEN generated_chars ELSE 0 END) AS preview_chars"
    for a in ([agent] if agent else ops.agents()):
        paths=[ops.data_path(a).with_name('token-usage.sqlite3')]
        paths.extend(sorted((ops.directory/'retired-usage').glob(str(a)+'-*.sqlite3')))
        for path in paths:
            try:
                if not path.is_file():continue
                with closing(connect(path,True)) as db:
                    if not db.execute('SELECT 1 FROM sqlite_master WHERE type=\'table\' AND name=?',(table,)).fetchone():continue
                    first=db.execute('SELECT MIN(created) FROM '+table+' WHERE agent=?'+(' AND kind=?' if resource!='token' else ''),[a]+([resource] if resource!='token' else [])).fetchone()[0]
                    if first is not None:first_recorded=min(first_recorded,first) if first_recorded is not None else first
                    if a not in ledger_apps:ledger_apps.append(a)
                    args=[a,start.timestamp(),end.timestamp()]
                    where='agent=? AND created>=? AND created<?'
                    if resource!='token':where+=' AND kind=?';args.append(resource)
                    if user:where+=' AND user=?';args.append(user)
                    total=dict(db.execute('SELECT '+select+' FROM '+table+' WHERE '+where,args).fetchone())
                    for key in summary:summary[key]+=total.get(key) or 0
                    unattributed+=db.execute('SELECT COUNT(*) FROM '+table+' WHERE '+where+" AND user=''",args).fetchone()[0]
                    column=GROUPS[group]
                    sql='SELECT '+(column+' AS dimension,' if column else '')+select+' FROM '+table+' WHERE '+where
                    if column:sql+=' GROUP BY '+column
                    rows=db.execute(sql+' LIMIT 5001',args).fetchall()
                    if len(rows)>5000:raise Rejected('分组过多，请缩小日期范围或选择一个应用。')
                    for row in rows:
                        value=dict(row)
                        if not value['requests']:continue
                        value.update(agent=a,dimension=value.get('dimension',str(a)))
                        items.append(value)
            except (sqlite3.Error,OSError):unavailable.append(a)
    merged={}
    for item in items:
        key=(item['agent'],item['dimension'])
        if key not in merged:merged[key]=dict(item)
        else:
            for field in summary:merged[key][field]=(merged[key].get(field) or 0)+(item.get(field) or 0)
    items=list(merged.values())
    def measured(value):
        if resource=='token':
            for field,count in zip(FIELDS,known):
                if not value[count]:value[field]=None
        return value
    items.sort(key=lambda v:(-(v.get('total_tokens' if resource=='token' else 'image_count' if resource=='image' else 'generated_chars') or 0),v['agent'],v['dimension']))
    return dict(summary=measured(summary),items=[measured(v) for v in items[(page-1)*100:page*100]],
        count=len(items),page=page,page_size=100,group=group,resource=resource,unavailable=unavailable,ledger_apps=ledger_apps,
        unattributed_requests=unattributed,first_recorded=first_recorded,
        start=start.date().isoformat(),end=(end-timedelta(days=1)).date().isoformat())
