"""Isolated production observer. Its only write store is the separate canary journal."""
from datetime import datetime
import json
import logging
import os
import sys
import time
from uuid import uuid4

import httpx

from .cost_console import import_receipt
from .cost_meter_worker import synchronize
from .cost_native_audit import audit, read_native
from .database import SessionLocal, engine
from .models import Base, CostReceipt, CostSyncState, PurchaseRate, now

OWNER = 'starbridge-v101c-gray-fixture'
CHANNEL = 'formal-siliconrouter-canary'
MODEL = 'deepseek-v4-flash-v2'


def dispatch(key):
    """One explicit operator test; there is no model traffic in the observer loop."""
    call_id = str(uuid4())
    started = now().isoformat()
    with httpx.Client(timeout=60, follow_redirects=False) as client:
        response = client.post('http://litellm.litellm.svc.cluster.local:4000/v1/chat/completions',
            headers={'Authorization':'Bearer '+key, 'x-litellm-call-id':call_id}, json={
                'model':MODEL, 'user':OWNER, 'max_tokens':48, 'stream':False,
                'messages':[{'role':'user','content':'Reply with exactly one word: OK'}]})
    response.raise_for_status()
    completion = response.json()
    usage = {field:completion['usage'][field] for field in ('prompt_tokens','completion_tokens','total_tokens')}
    event = {'event_id':call_id, 'attempt_id':call_id, 'call_id':call_id, 'owner_id':OWNER, 'channel':CHANNEL,
             'model':completion.get('model') or MODEL, 'response_id':completion['id'], 'usage':usage,
             'started_at':started, 'finished_at':now().isoformat(), 'status':'complete', 'usage_state':'observed'}
    with SessionLocal() as db:
        import_receipt(db, event)
        db.commit()
    print(json.dumps({'response_id':event['response_id'],'usage':usage}),flush=True)


def main():
    if engine.dialect.name != 'sqlite' or engine.url.database != '/state/observer.db':
        raise SystemExit('The gray observer requires its own isolated state store')
    Base.metadata.create_all(engine, tables=[PurchaseRate.__table__, CostReceipt.__table__, CostSyncState.__table__])
    if sys.argv[1:] == ['dispatch']:
        dispatch(sys.stdin.read().strip())
        return
    since = datetime.fromisoformat(os.environ['NATIVE_AUDIT_SINCE'])
    previous = None
    while True:
        try:
            with SessionLocal() as db:
                original = audit(db, lambda: read_native(os.environ['NATIVE_AUDIT_DSN']), OWNER, CHANNEL, since)
                meter = synchronize(db)
            current = {'original':original, 'meter':meter}
            if current != previous:
                print(json.dumps(current),flush=True)
                previous = current
        except Exception as error:
            logging.error('Gray observer failed: %s', type(error).__name__)
        time.sleep(30)


if __name__ == '__main__':
    main()
