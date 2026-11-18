"""No live RIPE calls or credits used by tests."""
import importlib.util
from pathlib import Path
import pytest

PATH = Path(__file__).resolve().parents[1] / 'scripts' / 'ripe_measurements.py'
spec = importlib.util.spec_from_file_location('ripe_measurements', PATH)
ripe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ripe)


def test_store_new_ids_preserves_other_env_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(ripe, 'ROOT', tmp_path)
    env = tmp_path / '.env'
    env.write_text('LAB_TOKEN=keep-secret\nATLAS_MEASUREMENTS=region-a=100,region-b=200\nLOCAL_AUTO_MITIGATION=false\n', encoding="utf-8")
    ripe.save_ids(env, 301, 302)
    content = env.read_text(encoding="utf-8")
    assert content.count('ATLAS_MEASUREMENTS=') == 1
    assert 'ATLAS_MEASUREMENTS=region-a=301,region-b=302' in content
    assert 'LAB_TOKEN=keep-secret' in content
    assert 'LOCAL_AUTO_MITIGATION=false' in content
    backups = list((tmp_path/'data'/'config-backups').glob('*.bak'))
    assert len(backups) == 1 and 'region-a=100' in backups[0].read_text(encoding="utf-8")


def test_repeated_save_does_not_create_another_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(ripe, 'ROOT', tmp_path)
    env = tmp_path/'.env'
    assert ripe.save_ids(env, 301, 302) is not None
    assert ripe.save_ids(env, 301, 302) is None
    assert not (tmp_path/'data'/'config-backups').exists()


@pytest.mark.parametrize('a,b', [(0,2), (-1,2), (5,5)])
def test_invalid_ids_rejected(tmp_path, a, b):
    with pytest.raises(ValueError):
        ripe.save_ids(tmp_path/'.env', a, b)


def test_status_metadata_parse(monkeypatch):
    monkeypatch.setattr(ripe, 'read_json', lambda url: {'type':'ping','target':'198.51.100.1','status':{'name':'Ongoing'}})
    result=ripe.metadata(1234)
    assert ripe.state(result)=='Ongoing'
