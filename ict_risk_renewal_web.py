"""Explicit authenticated dashboard renewal. GET never renews or polls broker.

POST prepare is read-only apart from inactive drafts. POST install is the only
active write, following current-rule confirmation, CSRF and password re-entry.
"""
import fcntl
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import sqlite3
import threading
import time
import account_risk_renewal as core

COOKIE='__Host-ict-risk-renewal'
ORIGIN='https://agent-50k-production.up.railway.app'

class Renewal:
    def __init__(self,root,password):
        self.root=Path(root)/'operator'/'risk_renewal_web';self.root.mkdir(parents=True,exist_ok=True)
        self.password=password
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, expires REAL, state TEXT, updated REAL, result TEXT, failures INTEGER DEFAULT 0)')
    def db(self):
        db=sqlite3.connect(self.root/'requests.sqlite3',timeout=5);db.row_factory=sqlite3.Row;return db
    def identity(self,token):return hashlib.sha256(token.encode()).hexdigest()
    def row(self,token):
        with self.db() as db:r=db.execute('SELECT * FROM requests WHERE id=?',(self.identity(token),)).fetchone()
        return dict(r) if r else None
    def session(self,token):
        row=self.row(token) if token else None
        if row and (row['expires']>time.time() or row['state'] in ('PREPARING','INSTALLING','UNKNOWN')):return token,row
        token=secrets.token_urlsafe(32);now=time.time()
        with self.db() as db:db.execute('INSERT INTO requests VALUES(?,?,?,?,?,0)',(self.identity(token),now+1200,'NEW',now,'{}'))
        return token,self.row(token)
    def status(self,token):
        row=self.row(token)
        if row['state'] in ('INSTALLING','PREPARING') and time.time()-row['updated']>180:
            stage='UNKNOWN' if row['state']=='INSTALLING' else 'ERROR'
            self.save(row['id'],stage,{'error':'Request interrupted. Check account status before any further action; contact your operator.'})
            row=self.row(token)
        try: account=core.view()
        except Exception as e:account={'state':'UNAVAILABLE','can_prepare':False,'error':self.error(e)}
        return dict(csrf=token,stage=row['state'],result=json.loads(row['result']),account=account)
    def save(self,key,state,result):
        with self.db() as db:db.execute('UPDATE requests SET state=?,updated=?,result=? WHERE id=?',(state,time.time(),json.dumps(result),key))
    @staticmethod
    def error(e):
        return str(e)[:400] if isinstance(e,ValueError) else 'Check failed ('+type(e).__name__+'). No successful renewal confirmed.'
    def start(self,token,action,body):
        key=self.identity(token);now=time.time()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM requests WHERE id=?',(key,)).fetchone()
            core.require(row is not None and row['expires']>now,'Confirmation expired. Refresh the page.')
            core.require(body.get('confirmed') is True,'Explicit confirmation is required.')
            core.require(not db.execute("SELECT 1 FROM requests WHERE state='UNKNOWN' OR (id!=? AND state IN ('PREPARING','INSTALLING'))",(key,)).fetchone(),'Another renewal is in progress or has an unknown outcome; check status first.')
            if action=='prepare':
                core.require(row['state'] in ('NEW','ERROR'),'Check already submitted. Wait for its result.')
                core.require(isinstance(body.get('rules_hash'),str) and len(body['rules_hash'])==64,'Reopen current rules before confirming.')
                core.require(not db.execute("SELECT 1 FROM requests WHERE state!='NEW' AND updated>?",(now-60,)).fetchone(),'Wait one minute before another account check.')
                stage='PREPARING'
            else:
                core.require(row['state']=='PREPARED','A fresh successful check is required before renewal.')
                core.require(row['failures']<5,'Too many password attempts; wait for this confirmation to expire.')
                if not hmac.compare_digest(str(body.get('password','')),self.password):
                    db.execute('UPDATE requests SET failures=failures+1 WHERE id=?',(key,));db.commit()
                    raise ValueError('Incorrect dashboard password.')
                core.require(body.get('accept_shared_activation') is True,'Confirm that shared renewal may unblock already-armed strategies.')
                stage='INSTALLING'
            db.execute('UPDATE requests SET state=?,updated=?,result=? WHERE id=?',(stage,now,'{}',key))
        # No password, CSRF token or auth headers enter the durable job record.
        thread=threading.Thread(target=self.work,args=(key,action,body.get('rules_hash')),daemon=True,name='operator-risk-renewal')
        thread.start()
        return dict(stage=stage)
    def work(self,key,action,rules_hash):
        # Same lock as the terminal helper; no concurrent renewal via another UI.
        lockpath=self.root.parent/'risk_renewal'/'operator.lock';lockpath.parent.mkdir(parents=True,exist_ok=True)
        folder=self.root/key;folder.mkdir(exist_ok=True)
        try:
            with lockpath.open('a') as lock:
                fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                result=core.prepare_stage(folder,rules_hash) if action=='prepare' else core.install_stage(folder)
                self.save(key,'PREPARED' if action=='prepare' else 'APPLIED',result)
        except Exception as e:
            self.save(key,'ERROR',{'error':self.error(e)})

    def reset(self,token):
        key=self.identity(token)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM requests WHERE id=?',(key,)).fetchone()
            core.require(row is not None and row['state'] in ('NEW','ERROR','PREPARED','APPLIED'),'Cannot reset an in-progress or uncertain request.')
            db.execute("UPDATE requests SET state='ABANDONED' WHERE id=?",(key,))
        return self.session(None)[0]

def register(app,private,config):
    from flask import request,jsonify
    password=config.get('AB_CONTEXT_SOURCE_PASSWORD') or config.get('TANJA_DASHBOARD_PASSWORD','')
    service=Renewal(config.get('DATA_DIR','/data/builder50'),password)
    def state():
        token,row=service.session(request.cookies.get(COOKIE))
        response=jsonify(service.status(token))
        response.set_cookie(COOKIE,token,secure=True,httponly=True,samesite='Strict',path='/',max_age=1200)
        response.headers['X-Frame-Options']='DENY'
        return response
    def action(kind):
        try:
            core.require(request.headers.get('Origin')==ORIGIN,'Same-origin confirmation required.')
            core.require(request.headers.get('Sec-Fetch-Site','same-origin')=='same-origin','Cross-site request refused.')
            core.require(request.is_json and request.content_length is not None and request.content_length<=4096,'Small JSON confirmation required.')
            body=request.get_json();token=request.cookies.get(COOKIE,'')
            core.require(isinstance(body,dict) and token and hmac.compare_digest(request.headers.get('X-Risk-CSRF',''),token),'Invalid confirmation token.')
            if kind=='reset':
                token=service.reset(token)
                response=jsonify(service.status(token))
                response.set_cookie(COOKIE,token,secure=True,httponly=True,samesite='Strict',path='/',max_age=1200)
                return response
            result=service.start(token,kind,body)
            response=jsonify(result);response.status_code=202;return response
        except (ValueError,TypeError) as e:
            response=jsonify({'error':service.error(e)});response.status_code=409;return response
    app.add_url_rule('/ict-shadow/risk-renewal','ict_risk_renewal_state',private(state))
    app.add_url_rule('/ict-shadow/risk-renewal/prepare','ict_risk_renewal_prepare',private(lambda:action('prepare')),methods=['POST'])
    app.add_url_rule('/ict-shadow/risk-renewal/install','ict_risk_renewal_install',private(lambda:action('install')),methods=['POST'])
    app.add_url_rule('/ict-shadow/risk-renewal/reset','ict_risk_renewal_reset',private(lambda:action('reset')),methods=['POST'])
    return service
