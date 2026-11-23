"""Bounded GET transport. Exact private origin, no redirects, no credential logging."""
import http.client,json,re,socket,time
from urllib.parse import urlsplit

class ReadFailure(Exception):pass

class PrivateReader:
    def __init__(self,origin,token,*,allow_loopback=False,timeout=4,max_bytes=4_000_000):
        u=urlsplit(origin)
        local=allow_loopback and u.hostname in ('127.0.0.1','localhost','::1')
        if u.scheme!='http' or u.username or u.password or u.path not in ('','/') or u.query or u.fragment or not (local or re.fullmatch(r'[a-z0-9-]+\.railway\.internal',u.hostname or '')):raise ValueError('PRIVATE_ORIGIN_REQUIRED')
        if not isinstance(token,str) or len(token)<32 or '\n' in token or '\r' in token:raise ValueError('SCOPED_SERVICE_TOKEN_REQUIRED')
        self.host=u.hostname;self.port=u.port or 8080;self.token=token;self.timeout=timeout;self.max_bytes=max_bytes
    def get(self,path):
        if not path.startswith(('/internal/ict-shadow/candles?','/ict-shadow/shadow-mvp/')) or any(x in path for x in ('\r','\n','..','://')):raise ValueError('READ_PATH_NOT_ALLOWED')
        conn=http.client.HTTPConnection(self.host,self.port,timeout=self.timeout)
        deadline=time.monotonic()+self.timeout
        try:
            conn.request('GET',path,headers={'Authorization':'Bearer '+self.token,'Accept':'application/json','Connection':'close'})
            r=conn.getresponse()
            # Never follow redirects or propagate response body from a failed service.
            if r.status!=200:raise ReadFailure('UPSTREAM_HTTP_'+str(r.status))
            chunks=[];size=0
            while True:
                remaining=deadline-time.monotonic()
                if remaining<=0:raise ReadFailure('UPSTREAM_DEADLINE')
                if conn.sock:conn.sock.settimeout(remaining)
                chunk=r.read1(min(65536,self.max_bytes+1-size))
                if not chunk:break
                chunks.append(chunk);size+=len(chunk)
                if size>self.max_bytes:raise ReadFailure('UPSTREAM_RESPONSE_LIMIT')
            data=b''.join(chunks)
            return data,r.getheader('Content-Type','application/json')
        except (OSError,http.client.HTTPException):raise ReadFailure('READ_TIMEOUT_OR_DISCONNECT') from None
        finally:conn.close()
