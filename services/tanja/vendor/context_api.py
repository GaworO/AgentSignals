"""Single-snapshot OpenAI Responses adapter. Dry-run by default; no broker code.

API contract source (checked 2026-10-07):
https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses
The caller selects an accessible model; no model ID or API credential is assumed.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import urllib.error
import urllib.request
from context_layer import CLAIMS, canonical_hash, review_response

ROOT=Path(__file__).resolve().parent
DOCS='https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses'


def object_schema(properties):
    return dict(type='object',properties=properties,required=list(properties),additionalProperties=False)


def output_schema():
    string={'type':'string'}
    nullable_bool={'type':['boolean','null']}
    claim=object_schema(dict(value=nullable_bool,evidence_ids={'type':'array','items':string},reason=string))
    smt=object_schema(dict(reference_es=string,reference_mnq=string,current_es=string,current_mnq=string,
                           side={'type':'string','enum':['low','high']}))
    selection=object_schema(dict(window_id=string,origin_bar_id=string,side={'type':'string','enum':['low','high']}))
    return object_schema(dict(schema_version={'type':'integer','enum':[2]},packet_id=string,as_of={'type':'integer'},
      bias={'type':'string','enum':['long','short','neutral','unknown']},
      decision={'type':'string','enum':['wait','candidate','abstain']},
      setup={'type':'string','enum':['inversion','order_block','breaker','unknown']},requires_smt=nullable_bool,
      selected_trigger_id={'type':['string','null']},claims=object_schema({name:claim for name in CLAIMS}),
      missing_context={'type':'array','items':string},rationale=string,
      smt_selection={'anyOf':[smt,{'type':'null'}]},range_selection={'anyOf':[selection,{'type':'null'}]}))


def make_request(packet,model,prompt):
    if not isinstance(model,str) or not model.strip(): raise ValueError('An explicitly configured API model is required')
    if packet.get('labels_included') is not False or packet.get('input_mode')!='market_only':
        raise ValueError('Only label-free market packets may be sent')
    if canonical_hash({k:v for k,v in packet.items() if k!='packet_id'})!=packet.get('packet_id'):
        raise ValueError('Packet hash mismatch')
    return dict(model=model,store=False,max_output_tokens=12000,
      input=[dict(role='system',content=prompt),dict(role='user',content=json.dumps(packet,separators=(',',':'),allow_nan=False))],
      text=dict(format=dict(type='json_schema',name='tanya_context',strict=True,schema=output_schema())))


def extract_response(raw):
    if raw.get('status')!='completed':
        raise ValueError('Model response incomplete or failed; not a trading decision')
    texts=[]
    for item in raw.get('output',[]):
        if item.get('type')!='message': continue
        for content in item.get('content',[]):
            if content.get('type')=='refusal': raise ValueError('Model refused; no context decision')
            if content.get('type')=='output_text': texts.append(content['text'])
    if len(texts)!=1: raise ValueError('Expected exactly one structured model response')
    result=json.loads(texts[0])
    if not isinstance(result,dict): raise ValueError('Context must be an object')
    return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): return None


def call_api(payload,api_key):
    if not api_key: raise ValueError('OPENAI_API_KEY is not configured locally')
    req=urllib.request.Request('https://api.openai.com/v1/responses',
      data=json.dumps(payload,allow_nan=False).encode(),method='POST',
      headers={'Authorization':'Bearer '+api_key,'Content-Type':'application/json'})
    opener=urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(req,timeout=45) as response: return json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError('OpenAI HTTP error '+str(exc.code)+'; no automatic retries or decisions') from None
    except urllib.error.URLError:
        raise RuntimeError('OpenAI connection failed; no automatic retries or decisions') from None


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('packet')
    parser.add_argument('--model',default=os.environ.get('OPENAI_MODEL'))
    parser.add_argument('--output-dir',required=True)
    parser.add_argument('--send',action='store_true',help='Send this market-only packet to OpenAI; requires local OPENAI_API_KEY')
    args=parser.parse_args()
    packet=json.loads(Path(args.packet).read_text());prompt=(ROOT/'context_prompt.txt').read_text()
    payload=make_request(packet,args.model,prompt)
    # A new directory prevents silently overwriting a previous experiment.
    dest=Path(args.output_dir);dest.mkdir(parents=True,exist_ok=False)
    save=lambda name,obj:(dest/name).write_text(json.dumps(obj,indent=2,allow_nan=False))
    save('request.json',payload)
    metadata=dict(created_at=datetime.now(timezone.utc).isoformat(),packet_id=packet['packet_id'],
      requested_model=args.model,prompt_sha256=canonical_hash(prompt),request_sha256=canonical_hash(payload),
      source_docs=DOCS,mode='api' if args.send else 'dry_run',api_called=False,
      dataset_role='development',blinded=False,executable=False)
    if not args.send:
        save('run.json',metadata)
        print(json.dumps(dict(status='request_prepared',api_called=False,output_dir=str(dest))))
        return
    try:
        key=os.environ.get('OPENAI_API_KEY')
        if not key: raise ValueError('Configure OPENAI_API_KEY locally; do not paste it into chat')
        metadata['api_called']=True
        raw=call_api(payload,key)
        save('api_response.json',raw)
        parsed=extract_response(raw)
        save('context_response.json',parsed)
        review=review_response(packet,parsed)
        save('review.json',review)
        save('conditional_shadow.json',review_response(packet,parsed,True))
        metadata.update(returned_model=raw.get('model'),response_id=raw.get('id'),usage=raw.get('usage'),status='validated')
    except (ValueError,RuntimeError,TimeoutError) as exc:
        metadata.update(status='failed',error=str(exc));save('run.json',metadata)
        raise SystemExit(str(exc))
    save('run.json',metadata)
    print(json.dumps(dict(status=metadata['status'],state=review['state'],executable=False)))

if __name__=='__main__': main()
