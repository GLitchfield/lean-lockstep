"""At-least-once durable delivery. Inbox acknowledgement is not a model wake-up."""
from __future__ import annotations

import json
import time

from lockstep.errors import UsageError
from lockstep.store import Store


def pending(cfg, agent):
    with Store(cfg) as st:
        rows = st.conn.execute("SELECT * FROM deliveries WHERE agent=? AND status<>'acknowledged' ORDER BY created_at", (agent,)).fetchall()
    return [{**dict(r), 'payload': json.loads(r['payload_json'])} for r in rows]


def claim(cfg, agent, consumer, lease_seconds=60):
    if not consumer or not 1 <= lease_seconds <= 300:
        raise UsageError('consumer identity and a 1–300 second lease required')
    with Store(cfg) as st:
        st.conn.execute('BEGIN IMMEDIATE')
        paused = st.conn.execute('SELECT value FROM automation_meta WHERE key=?', ('delivery-paused:' + agent,)).fetchone()
        if paused and paused[0] == '1':
            return None
        row = st.conn.execute("SELECT * FROM deliveries WHERE agent=? AND status<>'acknowledged' AND (lease_until IS NULL OR lease_until<?) ORDER BY created_at LIMIT 1", (agent, time.time())).fetchone()
        if not row:
            return None
        st.conn.execute("UPDATE deliveries SET consumer=?,lease_until=?,attempts=attempts+1,status='leased' WHERE id=?", (consumer, time.time()+lease_seconds, row['id']))
        return {'event_id': row['id'], 'payload': json.loads(row['payload_json']), 'attempt': row['attempts']+1}


def ack(cfg, agent, event_id, consumer):
    with Store(cfg) as st:
        st.conn.execute('BEGIN IMMEDIATE')
        row = st.conn.execute('SELECT * FROM deliveries WHERE id=? AND agent=?', (event_id, agent)).fetchone()
        if not row or row['consumer'] != consumer:
            raise UsageError('event is not leased to this consumer and agent')
        st.conn.execute("UPDATE deliveries SET status='acknowledged',acknowledged_at=?,lease_until=NULL WHERE id=?", (time.time(), event_id))
    return {'event_id': event_id, 'acknowledged': True}


def pause(cfg, agent, paused=True):
    with Store(cfg) as st:
        st.conn.execute('INSERT OR REPLACE INTO automation_meta VALUES (?,?)', ('delivery-paused:' + agent, '1' if paused else '0'))
    return {'agent': agent, 'paused': paused, 'compute_cancelled': False}
