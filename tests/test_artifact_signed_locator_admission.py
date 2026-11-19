from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import pytest
from pydantic import ValidationError

from nika_core.artifacts import ArtifactRegistry, ArtifactRegistryError
from nika_core.data.sqlite import SQLiteStore

_SIGNED_AUTHORITY_KEYS = (
    'X-Amz-Signature',
    'X-Amz-Credential',
    'X-Amz-Security-Token',
    'X-Goog-Signature',
    'X-Goog-Credential',
    'X-Goog-Security-Token',
    'AWSAccessKeyId',
    'GoogleAccessId',
    'sig',
    'signature',
)
_SIGNED_METADATA_AUTHORITY_KEYS = tuple(
    key for key in _SIGNED_AUTHORITY_KEYS
    if key not in {'AWSAccessKeyId', 'GoogleAccessId', 'signature'}
)


def _register(registry, reference, *, metadata=None):
    return registry.register_reference(
        workspace_id='workspace-signed-locator',
        idempotency_key='signed-locator-input',
        reference=reference,
        sha256='a' * 64,
        size_bytes=1,
        kind='evidence',
        metadata=metadata,
    )


def _assert_no_records(store):
    with store.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM artifact_registry_records').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM artifact_registry_verifications').fetchone()[0] == 0


@pytest.mark.parametrize('key', _SIGNED_AUTHORITY_KEYS)
def test_signed_storage_authority_is_denied_before_publication(tmp_path: Path, key):
    store = SQLiteStore(tmp_path / 'Сховище підписаних посилань.db')
    registry = ArtifactRegistry.from_store(store)
    synthetic = uuid4().hex
    reference = f'https://example.invalid/object?{key}={synthetic}'
    with pytest.raises(ValidationError, match='credential material'):
        _register(registry, reference)
    _assert_no_records(store)


@pytest.mark.parametrize('key', ('x_aMz_sIgNaTuRe', 'SIG', 'x-goog-security_token'))
@pytest.mark.parametrize('encoding_layers', (1, 2))
def test_signed_authority_key_case_and_percent_aliases_fail_closed(
    tmp_path: Path, key, encoding_layers,
):
    store = SQLiteStore(tmp_path / 'state.db')
    registry = ArtifactRegistry.from_store(store)
    # Encode separators and letters, rather than relying on quote's safe letters.
    encoded_key = ''.join(f'%{byte:02X}' for byte in key.encode())
    for _ in range(encoding_layers - 1):
        encoded_key = quote(encoded_key, safe='')
    reference = f'https://example.invalid/object?{encoded_key}={uuid4().hex}'
    with pytest.raises(ValidationError, match='credential material'):
        _register(registry, reference)
    _assert_no_records(store)


@pytest.mark.parametrize('reference_location', ('fragment', 'nested_url', 'opaque_assignment'))
def test_signed_locator_authority_cannot_hide_behind_other_reference_forms(
    tmp_path: Path, reference_location,
):
    store = SQLiteStore(tmp_path / 'state.db')
    registry = ArtifactRegistry.from_store(store)
    sensitive_url = f'https://example.invalid/object?X-Goog-Signature={uuid4().hex}'
    reference = {
        'fragment': sensitive_url.replace('?X-', '#X-'),
        'nested_url': 'https://example.invalid/redirect?target=' + quote(sensitive_url, safe=''),
        'opaque_assignment': 'blob:object/sig=' + uuid4().hex,
    }[reference_location]
    with pytest.raises(ValidationError, match='credential material'):
        _register(registry, reference)
    _assert_no_records(store)


def test_signed_url_in_metadata_is_also_denied_before_publication(tmp_path: Path):
    store = SQLiteStore(tmp_path / 'state.db')
    registry = ArtifactRegistry.from_store(store)
    with pytest.raises(ValidationError, match='credential material'):
        _register(
            registry, 'blob:safe-reference',
            metadata={'provenance': f'https://example.invalid/object?sig={uuid4().hex}'},
        )
    _assert_no_records(store)


@pytest.mark.parametrize('reference', (
    'https://example.invalid/object?version=3&download=1',
    'https://example.invalid/object?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Date=20261119',
    'https://example.invalid/object?sv=2026-01-01&sp=r&sr=b&se=2026-12-01',
    'https://example.invalid/object?signature_version=4&credential_id=public-reference',
    'blob:signature-report/version-3',
))
def test_ordinary_unsigned_references_remain_usable_after_reopen(tmp_path: Path, reference):
    store = SQLiteStore(tmp_path / 'state.db')
    registry = ArtifactRegistry.from_store(store)
    record = _register(registry, reference)
    reopened = ArtifactRegistry.from_store(SQLiteStore(store.path))
    assert reopened.get(record.artifact_id).locator == reference
    assert _register(reopened, reference).artifact_id == record.artifact_id


def test_rejected_signed_reference_does_not_poison_safe_retry_identity(tmp_path: Path):
    store = SQLiteStore(tmp_path / 'state.db')
    registry = ArtifactRegistry.from_store(store)
    with pytest.raises(ValidationError, match='credential material'):
        _register(registry, 'https://example.invalid/object?sig=' + uuid4().hex)
    _assert_no_records(store)
    safe = _register(registry, 'https://example.invalid/object?version=3')
    assert registry.get(safe.artifact_id) == safe


def test_legacy_signed_locator_is_rejected_on_read_without_rewriting_bytes(tmp_path: Path):
    store = SQLiteStore(tmp_path / 'state.db')
    registry = ArtifactRegistry.from_store(store)
    safe = _register(registry, 'blob:safe-reference')
    legacy = safe.model_dump(mode='json')
    legacy['locator'] = 'https://example.invalid/object?X-Amz-Signature=' + uuid4().hex
    legacy_json = json.dumps(legacy)
    with store.connection() as conn:
        conn.execute(
            'UPDATE artifact_registry_records SET record_json = ? WHERE artifact_id = ?',
            (legacy_json, safe.artifact_id),
        )
    reopened = ArtifactRegistry.from_store(SQLiteStore(store.path))
    with pytest.raises(ArtifactRegistryError, match='payload is invalid'):
        reopened.get(safe.artifact_id)
    with store.connection() as conn:
        stored = conn.execute(
            'SELECT record_json FROM artifact_registry_records WHERE artifact_id = ?',
            (safe.artifact_id,),
        ).fetchone()[0]
    assert stored == legacy_json


@pytest.mark.parametrize('key', _SIGNED_METADATA_AUTHORITY_KEYS)
def test_signed_authority_moved_into_metadata_key_is_not_persisted(tmp_path: Path, key):
    store = SQLiteStore(tmp_path / 'state.db')
    registry = ArtifactRegistry.from_store(store)
    with pytest.raises(ValidationError, match='secret material'):
        _register(registry, 'blob:safe-reference', metadata={key: uuid4().hex})
    _assert_no_records(store)


def test_non_authority_metadata_identifiers_remain_usable(tmp_path: Path):
    registry = ArtifactRegistry.from_store(SQLiteStore(tmp_path / 'state.db'))
    record = _register(registry, 'blob:safe-reference', metadata={
        'credential_id': 'public-reference', 'signature_version': '4',
        'signature': 'public-detached-signature', 'AWSAccessKeyId': 'public-key-id',
        'GoogleAccessId': 'public-key-id',
    })
    assert record.metadata == {
        'credential_id': 'public-reference', 'signature_version': '4',
        'signature': 'public-detached-signature', 'AWSAccessKeyId': 'public-key-id',
        'GoogleAccessId': 'public-key-id',
    }


@pytest.mark.parametrize('form', (
    'url', 'assignment', 'encoded_url', 'twice_encoded_url',
    'encoded_assignment', 'encoded_key_name',
))
def test_signed_url_or_assignment_used_as_metadata_key_is_not_persisted(tmp_path: Path, form):
    store = SQLiteStore(tmp_path / 'state.db')
    registry = ArtifactRegistry.from_store(store)
    synthetic = uuid4().hex
    url = 'https://e.invalid/o?sig=' + synthetic
    assignment = 'sig=' + synthetic
    key = {
        'url': url,
        'assignment': assignment,
        'encoded_url': quote(url, safe=''),
        'twice_encoded_url': quote(quote(url, safe=''), safe=''),
        'encoded_assignment': quote(assignment, safe=''),
        'encoded_key_name': 's%69g%3D' + synthetic,
    }[form]
    assert len(key) <= 120
    with pytest.raises(ValidationError, match='credential material'):
        _register(registry, 'blob:safe-reference', metadata={key: 'safe-label'})
    _assert_no_records(store)


@pytest.mark.parametrize('key', (
    'https://e.invalid/report?version=3',
    'https://e.invalid/o?signature_version=4',
    'https%3A%2F%2Fe.invalid%2Fo%3Fversion%3D3',
))
def test_ordinary_unsigned_url_metadata_keys_remain_usable(tmp_path: Path, key):
    store = SQLiteStore(tmp_path / 'state.db')
    registry = ArtifactRegistry.from_store(store)
    record = _register(registry, 'blob:safe-reference', metadata={key: 'safe-label'})
    reopened = ArtifactRegistry.from_store(SQLiteStore(store.path))
    assert reopened.get(record.artifact_id).metadata == {key: 'safe-label'}
