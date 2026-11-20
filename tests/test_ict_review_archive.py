import gc
import json
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
import ict_review_archive as archive


def populate(tmp_path,count=36,size=150000):
    folder=tmp_path/'ict_2022_nyam_20261120';folder.mkdir()
    values=[]
    for i in range(count):
        item={'id':f'ICT_INDEPENDENT_NYAM_20261120_{i}','requested_at':i,'packet':{'evidence':{'anchor':{'price':i}}},
              'decision':{'decision':'WAIT','plan':None},'original_sse':str(i)+'x'*size}
        (folder/(item['id']+'.json')).write_text(json.dumps(item));values.append(item)
    return values


def test_all_history_order_content_and_changed_file(tmp_path):
    values=populate(tmp_path,105,100)
    records=archive.Records(tmp_path)
    assert len(records)==105
    assert list(records)==list(reversed(values))
    assert records[-1]==values[0]
    item=dict(values[0],requested_at=200)
    (tmp_path/'ict_2022_nyam_20261120'/(item['id']+'.json')).write_text(json.dumps(item))
    assert next(iter(archive.Records(tmp_path)))==item


def test_corrupt_record_and_bounded_index(tmp_path,monkeypatch):
    populate(tmp_path,12,100)
    archive._INDEX.clear();monkeypatch.setattr(archive,'INDEX_LIMIT',5)
    bad=tmp_path/'ict_2022_nyam_20261120'/'ICT_INDEPENDENT_NYAM_20261120_bad.json';bad.write_text('{')
    assert len(archive.Records(tmp_path))==12
    assert len(archive._INDEX)<=5
    bad.write_text(json.dumps({'id':'repaired','requested_at':100}))
    assert next(iter(archive.Records(tmp_path)))['id']=='repaired'


def test_streaming_peak_is_bounded_and_concurrent_contents_equal(tmp_path):
    del_values=populate(tmp_path);del del_values;gc.collect()
    archive._INDEX.clear();tracemalloc.start()
    records=archive.Records(tmp_path)
    expected=[r['id'] for r in records]
    _,stream_peak=tracemalloc.get_traced_memory();tracemalloc.stop()
    gc.collect();tracemalloc.start()
    eager=[json.loads(p.read_text()) for p in (tmp_path/'ict_2022_nyam_20261120').glob('*.json')]
    _,eager_peak=tracemalloc.get_traced_memory();tracemalloc.stop()
    assert len(eager)==36 and eager_peak>stream_peak*4
    del eager
    with ThreadPoolExecutor(max_workers=8) as pool:
        results=list(pool.map(lambda _:[r['id'] for r in archive.Records(tmp_path)],range(16)))
    assert all(x==expected for x in results)
