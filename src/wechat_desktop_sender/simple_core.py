"""Original queue, durable state and batch engine, without the business receiver."""
from __future__ import annotations
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import json
import math
import re
import sqlite3
import threading
import time
from uuid import uuid4

SAFE_RETRY_REASONS = frozenset({'STOP_TASKBAR_STAGE_STOP_RECENT_USER_INPUT', 'STOPPED_BEFORE_CHILD','STOP_USER_CANCELLED'})


class ForwardError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(value, code):
    if not value:
        raise ForwardError(code)


def safe_code(error):
    value = getattr(error, 'code', '')
    return value if isinstance(value, str) and re.fullmatch(r'[A-Z0-9_:.\-]{1,100}', value) else type(error).__name__


@dataclass(frozen=True)
class Route:
    account_id: str
    account_root: str
    source_id: str
    target_id: str
    source_alias: str
    target_alias: str
    account_display: str = ''


class Ledger:
    """Only this application's new database. Exact text hashes are target-scoped."""
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY, account TEXT NOT NULL, source TEXT NOT NULL,
            target TEXT NOT NULL, content_hash TEXT NOT NULL, text TEXT NOT NULL,
            state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
            updated REAL NOT NULL, UNIQUE(account,target,content_hash));
          CREATE TABLE IF NOT EXISTS aliases (
            account TEXT NOT NULL, source TEXT NOT NULL, target TEXT NOT NULL,
            alias TEXT NOT NULL, content_hash TEXT NOT NULL, job INTEGER,
            PRIMARY KEY(account,source,target,alias), FOREIGN KEY(job) REFERENCES jobs(id));
          CREATE TABLE IF NOT EXISTS skips (
            id INTEGER PRIMARY KEY, account TEXT NOT NULL, target TEXT NOT NULL,
            fingerprint TEXT NOT NULL, reason TEXT NOT NULL,
            UNIQUE(account,target,fingerprint));
          CREATE TABLE IF NOT EXISTS receipts (
            id INTEGER PRIMARY KEY, job INTEGER NOT NULL, state TEXT NOT NULL,
            reason TEXT NOT NULL, at REAL NOT NULL, FOREIGN KEY(job) REFERENCES jobs(id));
          CREATE TABLE IF NOT EXISTS send_attempts (
            ticket TEXT PRIMARY KEY, job INTEGER NOT NULL, state TEXT NOT NULL,
            created REAL NOT NULL, updated REAL NOT NULL,
            FOREIGN KEY(job) REFERENCES jobs(id));
          CREATE TABLE IF NOT EXISTS route_state (
            account TEXT NOT NULL,source TEXT NOT NULL,target TEXT NOT NULL,
            paused INTEGER NOT NULL,reason TEXT NOT NULL,
            PRIMARY KEY(account,source,target));
        ''')
        self.db.commit()

    def close(self):
        self.db.close()

    def recover(self):
        with self.db:
            for row in self.db.execute("SELECT DISTINCT account,source,target FROM jobs WHERE state='SUBMITTING'").fetchall():
                self.db.execute('INSERT OR REPLACE INTO route_state VALUES(?,?,?,?,?)',(*tuple(row),1,'INTERRUPTED_SUBMISSION_NO_RETRY'))
            self.db.execute("UPDATE jobs SET state='UNKNOWN',reason='INTERRUPTED_SUBMISSION_NO_RETRY',updated=? WHERE state='SUBMITTING'", (time.time(),))
            self.db.execute("UPDATE send_attempts SET state='UNKNOWN',updated=? WHERE state='SUBMITTING'",(time.time(),))

    def pause_state(self,route):
        scope=(route.account_id,route.source_id,route.target_id)
        row=self.db.execute('SELECT paused,reason FROM route_state WHERE account=? AND source=? AND target=?',scope).fetchone()
        if row:return bool(row['paused']),row['reason']
        # One-time migration: old failed jobs require explicit recovery after restart.
        prior=self.db.execute("SELECT reason FROM jobs WHERE account=? AND source=? AND target=? AND state IN ('BLOCKED','UNKNOWN') ORDER BY updated DESC LIMIT 1",scope).fetchone()
        paused,reason=bool(prior),prior['reason'] if prior else ''
        with self.db:self.db.execute('INSERT INTO route_state VALUES(?,?,?,?,?)',(*scope,int(paused),reason))
        return paused,reason

    def resume_route(self,route):
        with self.db:self.db.execute('INSERT OR REPLACE INTO route_state VALUES(?,?,?,?,?)',
            (route.account_id,route.source_id,route.target_id,0,''))

    def _accept(self, route, event, not_before):
        require(event.get('account_id') == route.account_id and event.get('source_id') == route.source_id, 'EVENT_SCOPE_MISMATCH')
        stamp = event.get('occurred_at')
        require(type(stamp) in (int,float) and math.isfinite(stamp) and stamp >= not_before, 'EVENT_BEFORE_START')
        aliases = event.get('dedupe_aliases') or (event.get('event_id'),)
        require(bool(aliases) and all(isinstance(a,str) and a for a in aliases), 'STABLE_MESSAGE_ID_REQUIRED')
        text = event.get('text')
        require(event.get('message_type') == 'text' and isinstance(text,str), 'NON_TEXT_UNSUPPORTED')
        digest = sha256(text.encode('utf-8')).hexdigest()
        scope = (route.account_id, route.source_id, route.target_id)
        existing = []
        for alias in aliases:
            row = self.db.execute('SELECT content_hash,job FROM aliases WHERE account=? AND source=? AND target=? AND alias=?', (*scope,alias)).fetchone()
            if row:
                require(row['content_hash'] == digest, 'MESSAGE_ID_CONTENT_CONFLICT')
                existing.append(row['job'])
        row = self.db.execute('SELECT id FROM jobs WHERE account=? AND target=? AND content_hash=?', (route.account_id,route.target_id,digest)).fetchone()
        duplicate = bool(existing or row)
        if row:
            job = row['id']
        elif existing:
            job = existing[0]
        else:
            reason = ('EMPTY_TEXT_UNSUPPORTED' if not text else 'NUL_TEXT_UNSUPPORTED' if '\x00' in text
                      else 'TEXT_OVER_10000_UNSUPPORTED' if len(text)>10000 else '')
            now = time.time()
            cur = self.db.execute('INSERT INTO jobs(account,source,target,content_hash,text,state,reason,created,updated) VALUES(?,?,?,?,?,?,?,?,?)',
              (*scope,digest,text,'UNSUPPORTED' if reason else 'PENDING',reason,now,now))
            job = cur.lastrowid
        for alias in set(aliases):
            self.db.execute('INSERT OR IGNORE INTO aliases VALUES(?,?,?,?,?,?)', (*scope,alias,digest,job))
        return 'DUPLICATE' if duplicate else self.db.execute('SELECT state FROM jobs WHERE id=?',(job,)).fetchone()[0]

    def accept_batch(self, route, events, held=(), *, not_before):
        counts = {'PENDING':0,'DUPLICATE':0,'UNSUPPORTED':0}
        with self.db:
            for event in sorted(events, key=lambda x:x['occurred_at']):
                result = self._accept(route,event,not_before)
                counts[result] = counts.get(result,0)+1
            for item in held:
                fingerprint = sha256(json.dumps([list(item.get('dedupe_aliases',())),item.get('body_hash')],sort_keys=True).encode()).hexdigest()
                # Only an issue code is persisted, never unsupported media contents or URLs.
                reason = ','.join(x for x in item.get('issues',('NON_TEXT_MESSAGE',)) if re.fullmatch(r'[A-Z0-9_]{1,80}',x)) or 'NON_TEXT_MESSAGE'
                cur = self.db.execute('INSERT OR IGNORE INTO skips(account,target,fingerprint,reason) VALUES(?,?,?,?)',(route.account_id,route.target_id,fingerprint,reason))
                counts['UNSUPPORTED'] += cur.rowcount
        return counts

    def next_pending(self, route):
        row = self.db.execute("SELECT * FROM jobs WHERE account=? AND source=? AND target=? AND state='PENDING' ORDER BY created,id LIMIT 1",(route.account_id,route.source_id,route.target_id)).fetchone()
        return dict(row) if row else None

    def pending_batch(self, route):
        rows=self.db.execute("SELECT * FROM jobs WHERE account=? AND source=? AND target=? AND state='PENDING' ORDER BY created,id LIMIT 10",(route.account_id,route.source_id,route.target_id)).fetchall()
        return [dict(row) for row in rows]

    def begin(self, job):
        ticket=uuid4().hex
        with self.db:
            cur = self.db.execute("UPDATE jobs SET state='SUBMITTING',updated=? WHERE id=? AND state='PENDING'",(time.time(),job))
            require(cur.rowcount == 1,'JOB_ALREADY_CLAIMED')
            self.db.execute('INSERT INTO send_attempts VALUES(?,?,?,?,?)',(ticket,job,'SUBMITTING',time.time(),time.time()))
        return ticket

    def requeue_confirmed_blocked(self, route, check_receipt):
        """Explicit Resume only. Keep every old receipt and protected job intact."""
        count=0
        with self.db:
            rows=self.db.execute("SELECT * FROM jobs WHERE account=? AND source=? AND target=? AND state='BLOCKED' ORDER BY id",(route.account_id,route.source_id,route.target_id)).fetchall()
            for row in rows:
                job=dict(row)
                if job['reason'] not in SAFE_RETRY_REASONS:continue
                attempt=self.db.execute('SELECT ticket FROM send_attempts WHERE job=? ORDER BY created DESC,rowid DESC LIMIT 1',(job['id'],)).fetchone()
                job['attempt_id']=attempt['ticket'] if attempt else None
                if check_receipt(job) is not True:continue
                self.db.execute("UPDATE jobs SET state='PENDING',reason='',updated=? WHERE id=? AND state='BLOCKED'",(time.time(),job['id']))
                self.db.execute('INSERT INTO receipts(job,state,reason,at) VALUES(?,?,?,?)',
                    (job['id'],'RECOVERY_QUEUED','USER_RESUMED_CONFIRMED_NO_SUBMIT',time.time()))
                count+=1
        return count

    def finish(self, job, result):
        state = result.get('status')
        state = state if state in ('SUBMITTED_UNVERIFIED','BLOCKED','UNKNOWN') else 'UNKNOWN'
        reason = result.get('reason','')
        reason = reason if isinstance(reason,str) and re.fullmatch(r'[A-Z0-9_:.\-]{0,100}',reason) else 'UNCLASSIFIED_RESULT'
        with self.db:
            self.db.execute('UPDATE jobs SET state=?,reason=?,updated=? WHERE id=?',(state,reason,time.time(),job))
            self.db.execute("UPDATE send_attempts SET state=?,updated=? WHERE job=? AND state='SUBMITTING'",(state,time.time(),job))
            self.db.execute('INSERT INTO receipts(job,state,reason,at) VALUES(?,?,?,?)',(job,state,reason,time.time()))
            if state!='SUBMITTED_UNVERIFIED':
                route=self.db.execute('SELECT account,source,target FROM jobs WHERE id=?',(job,)).fetchone()
                self.db.execute('INSERT OR REPLACE INTO route_state VALUES(?,?,?,?,?)',(*tuple(route),1,reason))
        return state

    def stats(self, route):
        out = {r['state']:r['n'] for r in self.db.execute('SELECT state,count(*) AS n FROM jobs WHERE account=? AND target=? GROUP BY state',(route.account_id,route.target_id))}
        out['UNSUPPORTED'] = out.get('UNSUPPORTED',0)+self.db.execute('SELECT count(*) FROM skips WHERE account=? AND target=?',(route.account_id,route.target_id)).fetchone()[0]
        return out


class Engine:
    def __init__(self, route, cfg, receiver, ledger, sender, notice, *, notify=lambda **kw:None,
                 validate_route=lambda:None, safe_retry_check=lambda job:False,
                 before_send=lambda index,total:True, after_batch=lambda outcome:None, clock=time.time, stop=None):
        self.route,self.cfg,self.receiver,self.ledger = route,cfg,receiver,ledger
        self.sender,self.notice,self.notify,self.validate_route = sender,notice,notify,validate_route
        self.clock=clock
        self.stop=stop or threading.Event()
        self.cursor=receiver.initial_cursor()
        self.not_before=self.cursor.next_start
        self.next_poll=self.next_send=0
        self.send_paused=False
        self.pause_reason=''
        self.resume_requested=threading.Event()
        self.safe_retry_check=safe_retry_check
        self.before_send,self.after_batch=before_send,after_batch
        self.ledger.recover()
        self.send_paused,self.pause_reason=self.ledger.pause_state(self.route)

    def request_resume(self):
        """UI may signal recovery; only the serial worker touches queue or sender."""
        if self.stop.is_set():return False
        self.resume_requested.set()
        return True

    def tick(self):
        if self.stop.is_set(): return
        if self.resume_requested.is_set():
            self.resume_requested.clear()
            self.validate_route()
            count=self.ledger.requeue_confirmed_blocked(self.route,self.safe_retry_check)
            self.ledger.resume_route(self.route)
            self.send_paused=False
            self.pause_reason=''
            self.next_send=0
            self.notify(code='SEND_RESUMED',requeued=count,
                        protected_unknown=self.ledger.stats(self.route).get('UNKNOWN',0))
        now=self.clock()
        if now>=self.next_poll:
            batch=self.receiver.poll(self.cursor,now)
            if batch.status=='READY':
                counts=self.ledger.accept_batch(self.route,batch.events,batch.held,not_before=self.not_before)
                self.cursor=batch.next_cursor  # Advance only after the entire durable transaction.
                self.notify(code='RECEIVED',counts=counts)
            else: self.notify(code=batch.status)
            self.next_poll=self.clock()+self.cfg['poll_seconds']
        if self.stop.is_set() or self.send_paused or self.clock()<self.next_send: return
        batch=self.ledger.pending_batch(self.route)
        if not batch:return
        outcome='cancelled'
        try:
            self.validate_route()
            if not self.notice(self.stop,count=len(batch)) or self.stop.is_set():return
            self.validate_route()
            for index,job in enumerate(batch):
                if self.stop.is_set():break
                if index and self.stop.wait(self.cfg['send_seconds']):break
                if self.stop.is_set():break
                self.validate_route()
                if not self.before_send(index+1,len(batch)) or self.stop.is_set():break
                job['attempt_id']=self.ledger.begin(job['id'])
                self.notify(code='SENDING',job=job['id'])
                try: result=self.sender(job,self.route,self.stop)
                except BaseException: result={'status':'UNKNOWN','reason':'SENDER_EXCEPTION_NO_RETRY'}
                state=self.ledger.finish(job['id'],result)
                self.next_send=self.clock()+self.cfg['send_seconds']
                self.notify(code=state,job=job['id'],reason=result.get('reason',''))
                if state!='SUBMITTED_UNVERIFIED':
                    outcome='unknown' if state=='UNKNOWN' else 'failed'
                    self.send_paused=True
                    self.pause_reason=result.get('reason',state)
                    self.notify(code='SEND_PAUSED',reason=self.pause_reason,
                                resume_available=True,protected_unknown=self.ledger.stats(self.route).get('UNKNOWN',0))
                    break
    
            else:
                outcome='completed'
        except BaseException:
            outcome='failed'
            raise
        finally:
            self.after_batch(outcome)

    def run(self):
        try:
            while not self.stop.is_set():
                self.tick()
                self.notify(code='STATS',stats=self.ledger.stats(self.route),
                            send_paused=self.send_paused,pause_reason=self.pause_reason)
                self.stop.wait(.2)
        except Exception as error:
            self.notify(code='ERROR_STOPPED',reason=safe_code(error))
        finally:
            self.receiver=None
            self.ledger.close()
            self.notify(code='STOPPED')
