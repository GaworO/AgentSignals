import json
import sqlite3
import pytest
from services.tanja.ict_agent.common import digest, encode
from services.tanja.ict_agent import retrieval_storage as rs


def database():
    db=sqlite3.connect(':memory:')
    db.execute('CREATE TABLE retrievals(id TEXT PRIMARY KEY,payload TEXT)')
    rs.initialize(db)
    return db


def test_notes_are_stored_once_and_exact_original_hashes_survive():
    db=database(); note={'text':'źródło '*10000,'rule_ids':['a']}
    for i in range(20):
        value=dict(records=[{'id':str(i)}],audit_notes=[note,note],event_sequence=[i])
        ident=rs.put(db,value)
        raw=db.execute('SELECT payload FROM retrievals WHERE id=?',(ident,)).fetchone()[0]
        assert encode(rs.unpack(db,raw,ident))==encode(value)
        assert len(raw)<1000
    assert db.execute('SELECT count(*) FROM retrieval_notes').fetchone()[0]==1
    assert db.execute('SELECT count(*) FROM retrievals').fetchone()[0]==20


def test_legacy_rows_remain_readable_and_are_not_rewritten():
    db=database();value={'records':[],'audit_notes':[{'text':'original'}]}
    raw=encode(value);ident=digest(value)
    db.execute('INSERT INTO retrievals VALUES(?,?)',(ident,raw))
    assert rs.unpack(db,raw,ident)==value
    assert rs.put(db,value)==ident
    assert db.execute('SELECT payload FROM retrievals').fetchone()[0]==raw


def test_missing_or_modified_note_fails_closed():
    db=database();value={'audit_notes':[{'text':'original'}]}
    ident=rs.put(db,value);raw=db.execute('SELECT payload FROM retrievals').fetchone()[0]
    db.execute('UPDATE retrieval_notes SET payload=?',(encode({'text':'edited'}),))
    with pytest.raises(ValueError,match='AUDIT_NOTE_HASH_MISMATCH'):rs.unpack(db,raw,ident)
    db.execute('DELETE FROM retrieval_notes')
    with pytest.raises(ValueError,match='AUDIT_NOTE_MISSING'):rs.unpack(db,raw,ident)


def test_modified_envelope_or_identity_fails_closed():
    db=database();value={'records':[],'audit_notes':[]};ident=rs.put(db,value)
    raw=db.execute('SELECT payload FROM retrievals').fetchone()[0]
    changed=json.loads(raw);changed['content']['records']=[{'id':'invented'}]
    with pytest.raises(ValueError,match='RETRIEVAL_HASH_MISMATCH'):rs.unpack(db,encode(changed),ident)
    with pytest.raises(ValueError,match='RETRIEVAL_ID_MISMATCH'):rs.unpack(db,raw,'wrong')
