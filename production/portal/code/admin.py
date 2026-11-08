"""Local administration: waitlist metadata only; no chats, passwords or credentials."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3

if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('command', choices=['waitlist','counts'])
    p.add_argument('--database', default='/home/ubuntu/wecom-kf/portal/data/portal.sqlite3')
    args = p.parse_args()
    with closing(sqlite3.connect(Path(args.database).as_uri()+'?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        if args.command == 'waitlist':
            data = [dict(row) for row in db.execute('SELECT username,waiting AS requested_at FROM accounts WHERE waiting IS NOT NULL AND subject IS NULL ORDER BY waiting')]
        else:
            data = {
                'registered':db.execute('SELECT COUNT(*) FROM accounts').fetchone()[0],
                'bound':db.execute('SELECT COUNT(*) FROM accounts WHERE subject IS NOT NULL').fetchone()[0],
                'waiting':db.execute('SELECT COUNT(*) FROM accounts WHERE waiting IS NOT NULL AND subject IS NULL').fetchone()[0]}
    print(json.dumps(data, ensure_ascii=False))
