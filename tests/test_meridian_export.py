"""Export boundaries; no private Meridian source or credentials enter bundles."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('export_meridian', ROOT / 'scripts/export_meridian.py')
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


def test_export_matches_loader_layout_and_contains_no_runtime_dependencies(tmp_path):
    out = tmp_path / 'plugin'
    manifest = exporter.export_plugin(out, origin='https://loom.example.com', key='io.example.loom',
                                      version='1.0.0', privacy_url='https://example.com/privacy')
    assert manifest['permissions'][-1] == 'network:loom.example.com'
    assert {e['type'] for e in manifest['extensions']} == {'board_view','item_view','dashboard_widget'}
    for extension in manifest['extensions']:
        assert (out / 'dist' / extension['client']['module']).is_file()
    for name, digest in json.loads((out / 'SHA256SUMS.json').read_text()).items():
        assert hashlib.sha256((out / name).read_bytes()).hexdigest() == digest
    assert (out / 'dist/client.js').read_bytes() == (ROOT / 'scripts/okf_loom/viewer/static/client.js').read_bytes()
    with pytest.raises(FileExistsError):
        exporter.export_plugin(out, origin='https://loom.example.com', key='io.example.loom',
                               version='1.0.0', privacy_url='https://example.com/privacy')


@pytest.mark.parametrize('origin', ['http://loom.example.com','https://127.0.0.1','https://localhost',
    'https://name.local','https://name.internal','https://user:secret@example.com',
    'https://example.com/prefix','https://example.com:8787','https://example.com?token=secret'])
def test_export_refuses_unsupported_destinations(origin):
    with pytest.raises(ValueError):
        exporter.public_origin(origin)
