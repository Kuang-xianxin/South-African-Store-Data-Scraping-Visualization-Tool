from pathlib import Path

from takealot_ops.erp.radar_code_version import materialized_code_fingerprint


def test_unrelated_handler_does_not_invalidate_but_projection_dependency_does(tmp_path):
    source = tmp_path / "src/takealot_ops/erp"
    source.mkdir(parents=True)
    for name in ("radar_materialized", "live_updates", "radar_warmup", "radar_code_version"):
        (source / f"{name}.py").write_text("VERSION = 1\n")
    web = source / "web.py"
    web.write_text("from takealot_ops.erp.helper import load\n"
                   "def competitors(): return local()\n"
                   "def local(): return load()\n"
                   "def homepage(): return 1\n")
    helper = source / "helper.py"
    helper.write_text("from takealot_ops.erp.deep import value\ndef load(): return value\n")
    deep = source / "deep.py"
    deep.write_text("value = 1\n")
    first = materialized_code_fingerprint(tmp_path)
    web.write_text(web.read_text().replace("homepage(): return 1", "homepage(): return 2"))
    assert materialized_code_fingerprint(tmp_path) == first
    deep.write_text("value = 2\n")
    second = materialized_code_fingerprint(tmp_path)
    assert second != first
    web.write_text(web.read_text().replace("local(): return load()", "local(): return load() + 1"))
    assert materialized_code_fingerprint(tmp_path) != second
    third = materialized_code_fingerprint(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config/nf_profit_model.json").write_text('{"schema_version": 2}')
    assert materialized_code_fingerprint(tmp_path) != third


def test_real_projection_fingerprint_can_resolve_transitive_imports():
    assert len(materialized_code_fingerprint(Path(__file__).resolve().parents[2])) == 64
