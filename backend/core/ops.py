"""Offline maintenance: backup, report and explicit restore. Never send or launch workers."""
import argparse, json, sqlite3
from pathlib import Path
from datetime import datetime
from backend.core import config


def backup(destination: Path):
    destination = destination.resolve()
    if destination.exists(): raise ValueError('Backup destination already exists')
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(config.DB_PATH.resolve().as_uri()+'?mode=ro', uri=True)
    target = sqlite3.connect(destination)
    try: source.backup(target)
    finally: target.close(); source.close()
    destination.chmod(0o600)
    return str(destination)


def report():
    db=sqlite3.connect(config.DB_PATH.resolve().as_uri()+'?mode=ro',uri=True)
    queries={
        'application_statuses': 'SELECT status,count(*) FROM applications GROUP BY status',
        'reconcile_submissions': "SELECT id,platform,status FROM applications WHERE status IN ('submitting','assisting','needs_human')",
        'failed_factchecks': "SELECT id,status FROM applications WHERE json_extract(answers,'$.factcheck.ok') = 0",
        'email_routing_candidates': "SELECT a.id,j.source,a.method FROM applications a JOIN jobs j ON j.id=a.job_id WHERE j.source IN ('hackernews','reddit') AND a.method='ats_form'",
        'duplicate_application_jobs': 'SELECT job_id,count(*) FROM applications GROUP BY job_id HAVING count(*)>1',
        'duplicate_urls': 'SELECT url,count(*) FROM jobs GROUP BY url HAVING count(*)>1',
    }
    try: return {key: db.execute(query).fetchall() for key,query in queries.items()}
    finally: db.close()


def restore(source: Path, destination: Path):
    """Restore to a NEW path; never overwrite an active DB or its WAL."""
    if destination.exists(): raise ValueError('Restore requires a new destination path')
    origin=sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True)
    try:
        if origin.execute('PRAGMA integrity_check').fetchone()[0] != 'ok': raise ValueError('Backup integrity check failed')
        target=sqlite3.connect(destination)
        try: origin.backup(target)
        finally: target.close()
    finally: origin.close()
    destination.chmod(0o600)


def retention(days=30):
    """Report old unreferenced files; never delete automatically."""
    import time
    db=sqlite3.connect(config.DB_PATH.resolve().as_uri()+'?mode=ro',uri=True)
    try:
        refs={str((config.ROOT / p).resolve()) for row in db.execute('SELECT resume_path,screenshot_before,screenshot_after FROM applications') for p in row if p}
        refs.update(str((config.ROOT / p).resolve()) for (p,) in db.execute('SELECT screenshot FROM events WHERE screenshot IS NOT NULL'))
    finally: db.close()
    cutoff=time.time()-days*86400
    return [str(p) for root in (config.ARTIFACTS,config.DATA/'llm_cache') if root.exists() for p in root.rglob('*') if p.is_file() and p.stat().st_mtime < cutoff and str(p.resolve()) not in refs]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['backup','report','restore','retention'])
    parser.add_argument('--path',type=Path); parser.add_argument('--destination',type=Path)
    args=parser.parse_args()
    if args.action=='backup':
        if not args.path: parser.error('--path is required')
        print(backup(args.path))
    elif args.action=='restore':
        if not args.path or not args.destination: parser.error('--path and --destination are required')
        restore(args.path,args.destination);print('Restored to new destination')
    else: print(json.dumps(report() if args.action=='report' else retention(),indent=2))

if __name__=='__main__': main()
