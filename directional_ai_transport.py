"""Single foreground Responses SSE request; no retries and no broker access.

Only a terminal response object is returned. Deltas never grant permission.
The original event bytes are retained for audit; API keys are never retained.
"""
import http.client
import json
import math
import time


class Response(dict):
    pass


def request(payload, key, seconds, *, connection=http.client.HTTPSConnection,
            clock=time.monotonic):
    if not math.isfinite(seconds) or not 0 < seconds <= 90:
        raise ValueError('invalid_ai_transport_deadline')
    if payload.get('stream') is not True or payload.get('store') is not False:
        raise ValueError('stateless_stream_required')
    deadline=clock()+seconds
    conn=connection('api.openai.com',timeout=min(10,seconds))
    def remaining():
        value=deadline-clock()
        if value<=0:raise TimeoutError('ai_review_deadline')
        return value
    try:
        body=json.dumps(payload,separators=(',',':')).encode()
        conn.request('POST','/v1/responses',body,
                     {'Authorization':'Bearer '+key,'Content-Type':'application/json','Accept':'text/event-stream'})
        sock=conn.sock
        sock.settimeout(remaining())
        response=conn.getresponse()
        if response.status!=200:raise ValueError('ai_http_'+str(response.status))
        if 'text/event-stream' not in response.getheader('Content-Type',''):
            raise ValueError('ai_stream_content_type')
        raw=[];data=[];size=0;response_id=None
        while True:
            sock.settimeout(remaining())
            line=response.readline(4_000_001-size)
            remaining()
            if not line:raise ValueError('ai_stream_ended_before_terminal')
            size+=len(line)
            if size>4_000_000:raise ValueError('response_too_large')
            raw.append(line)
            if line.strip():
                if line.startswith(b'data:'):data.append(line[5:].strip())
                continue
            if not data:continue
            event=json.loads(b'\n'.join(data));data=[]
            kind=event.get('type')
            if kind=='response.created':
                response_id=event.get('response',{}).get('id')
            if kind in ('response.completed','response.failed','response.incomplete'):
                value=event.get('response')
                if (not isinstance(value,dict) or not response_id
                        or value.get('id')!=response_id):
                    raise ValueError('ai_stream_response_identity')
                result=Response(value)
                result.response_text=b''.join(raw).decode('utf-8')
                result.response_body_format='original_sse_events_utf8'
                return result
            if kind=='error':raise ValueError('ai_stream_error')
    finally:
        conn.close()
