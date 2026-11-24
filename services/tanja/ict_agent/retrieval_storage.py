"""Lossless storage of repeated audit notes, addressed by canonical content hash."""
import json
from .common import digest, encode

CODEC = 'ict-retrieval-audit-notes-v1'


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS retrieval_notes(id TEXT PRIMARY KEY,payload TEXT NOT NULL)')


def pack(db, value):
    if 'audit_notes' not in value:
        return encode(value)
    notes = value['audit_notes']
    if not isinstance(notes, list):
        raise ValueError('AUDIT_NOTES_MUST_BE_LIST')
    ids = []
    for note in notes:
        ident = digest(note); raw = encode(note)
        old = db.execute('SELECT payload FROM retrieval_notes WHERE id=?', (ident,)).fetchone()
        if old and old[0] != raw:
            raise ValueError('AUDIT_NOTE_IMMUTABILITY_CONFLICT')
        db.execute('INSERT OR IGNORE INTO retrieval_notes VALUES(?,?)', (ident, raw))
        ids.append(ident)
    return encode(dict(storage_codec=CODEC, canonical_sha256=digest(value), audit_note_ids=ids,
                       content={k:v for k,v in value.items() if k != 'audit_notes'}))


def unpack(db, raw, expected_id=None):
    value = json.loads(raw)
    if value.get('storage_codec') == CODEC:
        notes = []
        for ident in value['audit_note_ids']:
            row = db.execute('SELECT payload FROM retrieval_notes WHERE id=?', (ident,)).fetchone()
            if row is None:
                raise ValueError('AUDIT_NOTE_MISSING:'+ident)
            note = json.loads(row[0])
            if digest(note) != ident:
                raise ValueError('AUDIT_NOTE_HASH_MISMATCH')
            notes.append(note)
        reconstructed = dict(value['content'], audit_notes=notes)
        if digest(reconstructed) != value['canonical_sha256']:
            raise ValueError('RETRIEVAL_HASH_MISMATCH')
        value = reconstructed
    if expected_id is not None and digest(value) != expected_id:
        raise ValueError('RETRIEVAL_ID_MISMATCH')
    return value


def put(db, value):
    ident = digest(value)
    old = db.execute('SELECT payload FROM retrievals WHERE id=?', (ident,)).fetchone()
    if old:
        if unpack(db, old[0], ident) != value:
            raise ValueError('RETRIEVAL_IMMUTABILITY_CONFLICT')
    else:
        db.execute('INSERT INTO retrievals VALUES(?,?)', (ident, pack(db, value)))
    return ident
