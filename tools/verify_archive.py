"""Offline archive integrity and syntax checks; never imports application services."""
from pathlib import Path
import ast,hashlib,json,re,sys
ROOT=Path(__file__).resolve().parents[1]
def main():
    manifest=json.loads((ROOT/'SOURCE_MANIFEST.json').read_text(encoding='utf8'))
    errors=[];python_files=0;json_files=0
    for row in manifest['source_files']:
        p=ROOT/row['path']
        if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest()!=row['sha256']:errors.append('hash:'+row['path'])
    for p in ROOT.rglob('*'):
        if not p.is_file() or '.git' in p.parts:continue
        rel=p.relative_to(ROOT).as_posix()
        if p.suffix=='.py':
            try:ast.parse(p.read_text(encoding='utf-8-sig'),filename=rel);python_files+=1
            except SyntaxError as e:errors.append(f'python:{rel}:{e.lineno}')
        if p.suffix=='.json':
            try:json.loads(p.read_text(encoding='utf-8-sig'));json_files+=1
            except (ValueError,UnicodeError):errors.append('json:'+rel)
        if p.suffix.lower() in {'.pem','.p12','.pfx','.key','.dpapi','.db'} or '.sqlite' in p.name or p.name=='.env':errors.append('forbidden:'+rel)
    source=(ROOT/'production/portal/code/register.html').read_text(encoding='utf8')
    for number,word in [(4,'稍等加载'),(5,'下次聊天')]:
        section=re.search(r'<section id="guide-step-'+str(number)+r'".*?</section>',source,re.S)
        if not section or word not in section.group():errors.append('onboarding-order:'+str(number))
    result={'ok':not errors,'manifest_files':len(manifest['source_files']),'python_sources_parsed':python_files,'json_sources_parsed':json_files,'errors':errors,'network_requests':0,'application_imports':0}
    print(json.dumps(result,indent=2));return 0 if result['ok'] else 1
if __name__=='__main__':raise SystemExit(main())
