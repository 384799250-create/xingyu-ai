"""Read confirmed profile names without network calls or altering pool placeholders."""
import sqlite3

def display_name(connection,agent,owner,fallback):
    # Receipts belong to one application AND its current user. Never take names
    # from a former occupant, another tenant, an unsaved draft or an uncertain API write.
    if not isinstance(owner,str) or not owner:return fallback
    try:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='suite_template_application_updates'").fetchone():return fallback
        rows=connection.execute('''SELECT name FROM suite_template_application_updates
          WHERE agent=? AND user=? AND status='done' AND name!=''
          ORDER BY updated DESC,rowid DESC LIMIT 1''',(agent,owner))
        row=rows.fetchone()
        if row:
            name=row[0]
            if (isinstance(name,str) and name.strip() and len(name.encode('utf8'))<=32
                    and all(ord(c)>=32 and ord(c)!=127 for c in name)):
                return name.strip()
    except (sqlite3.Error,UnicodeError,ValueError):
        # A locked/old app database must not break the operations inventory.
        pass
    return fallback


def assigned_name(root,agent,owner,fallback,database=None):
    """Display-only lookup for an already-authorized assignment."""
    from pathlib import Path
    from contextlib import closing
    root=Path(root).resolve()
    if type(agent) is not int or agent<1:return fallback
    path=Path(database) if database is not None else root/'apps'/str(agent)/'data/kf.sqlite3'
    try:
        allowed=path.resolve().is_relative_to(root) or database is not None and path.resolve().is_relative_to(Path('/var/lib/docker/volumes'))
        if not path.is_file() or path.is_symlink() or not allowed:return fallback
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)) as db:
            return display_name(db,agent,owner,fallback)
    except (sqlite3.Error,OSError,ValueError):return fallback
