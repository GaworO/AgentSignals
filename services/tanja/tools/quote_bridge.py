"""Forward provider-normalized JSONL quotes; no subscription or order client.

Each input line is data with symbol, contract, bid, ask, last and bid_at,
ask_at, last_at (actual UTC component event seconds). Never use bar closes or
CrossTrade WebSocket frame epoch as these source timestamps.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import sys
import time
from urllib.parse import urlsplit
from urllib.request import Request,build_opener

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from crosstrade_capture import NoRedirect


class QuoteBridge:
    def __init__(self,config):
        self.url=config.get('TANJA_QUOTE_INGEST_URL','')
        self.token=config.get('TANJA_QUOTE_FEED_TOKEN','')
        self.source=config.get('TANJA_QUOTE_SOURCE','')
        url=urlsplit(self.url)
        if (url.scheme!='https' and not (url.scheme=='http' and url.hostname in ('127.0.0.1','localhost','::1'))
            or not url.hostname or url.username or url.password or url.query or url.fragment or url.path!='/api/live-quotes'):
            raise ValueError('PRIVATE_HTTPS_QUOTE_INGEST_URL_REQUIRED')
        import re
        if not re.fullmatch(r'[A-Za-z0-9_-]{32,128}',self.token) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',self.source):
            raise ValueError('QUOTE_SOURCE_CONFIGURATION_REQUIRED')
        self.generation=secrets.token_hex(16);self.sequence=0;self.opener=build_opener(NoRedirect())

    def send(self,kind,data):
        event=dict(source=self.source,generation=self.generation,sequence=self.sequence+1,kind=kind,sent_at=time.time(),data=data)
        body=json.dumps(event,allow_nan=False).encode()
        request=Request(self.url,data=body,method='POST',headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'})
        with self.opener.open(request,timeout=5) as response:
            result=json.loads(response.read(4096))
            if not result.get('ok'):raise ValueError('QUOTE_NOT_ACKNOWLEDGED')
        self.sequence+=1


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',action='store_true',help='Connect and consume normalized quotes on stdin; default validates config only')
    args=parser.parse_args();bridge=QuoteBridge(dict(os.environ))
    if not args.run:
        print(json.dumps(dict(configured=True,connected=False,orders_enabled=False)));return
    bridge.send('connected',{})
    try:
        for line in sys.stdin:
            if len(line)>4096:raise ValueError('QUOTE_TOO_LARGE')
            bridge.send('quote',json.loads(line))
    finally:
        try:bridge.send('disconnected',{})
        except Exception:pass  # Component timestamps expire even if disconnect cannot be delivered.


if __name__=='__main__':
    try:main()
    except Exception:
        print('Quote bridge stopped; validate private source configuration and component clocks before reconnecting.',file=sys.stderr)
        sys.exit(1)
