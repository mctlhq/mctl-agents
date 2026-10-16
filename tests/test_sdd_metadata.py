"""T1-T7: .openspec.yaml handling (spike #655, OpenSpec v1.14.2 / f80d860)."""

import shutil
from pathlib import Path

import pytest
import yaml

from orchestrator.sdd.metadata import (
    FORBIDDEN_EXECUTION_KEYS,
    MetadataError,
    dump_change_metadata,
    load_change_metadata,
)

FIX = Path(__file__).parent / "fixtures" / "sdd" / "metadata"


def test_t1_valid_strict_no_warnings():
    m = load_change_metadata(FIX / "valid", strict=True)
    assert m is not None and m.warnings == []
    assert m.data["created"] == "2026-10-16"


def test_t2_unknown_key_warns_then_fails_strict():
    m = load_change_metadata(FIX / "unknown-key")
    assert m is not None and any("mystery" in w for w in m.warnings)
    with pytest.raises(MetadataError, match="mystery"):
        load_change_metadata(FIX / "unknown-key", strict=True)


def test_t3_x_keys_pass_strict_and_roundtrip(tmp_path):
    m = load_change_metadata(FIX / "x-key", strict=True)
    assert m is not None and set(m.extensions) == {"x-mctl-slug", "x-other"}
    dump_change_metadata(m.data, tmp_path)
    again = load_change_metadata(tmp_path, strict=True)
    assert again is not None and again.data == m.data


def test_t4_initiative_rules():
    with pytest.raises(MetadataError, match="initiative"):
        load_change_metadata(FIX / "init-x-key")
    with pytest.raises(MetadataError, match="kebab"):
        load_change_metadata(FIX / "init-bad-store")


def test_t5_missing_file_is_none():
    assert load_change_metadata(FIX / "missing") is None


def test_t6_skip_specs(tmp_path):
    with pytest.raises(MetadataError, match="skip_specs"):
        load_change_metadata(FIX / "skip-with-specs")
    m = load_change_metadata(FIX / "skip-no-specs", strict=True)
    assert m is not None and m.skip_specs
    shutil.copytree(FIX / "skip-no-specs", tmp_path / "c")
    (tmp_path / "c" / "specs").mkdir()
    assert load_change_metadata(tmp_path / "c") is not None


@pytest.mark.parametrize("key", sorted(FORBIDDEN_EXECUTION_KEYS))
def test_t7_forbidden_keys_rejected_non_strict(key, tmp_path):
    data = {"schema": "mctl-devloop", key: "x"}
    with pytest.raises(MetadataError, match=key):
        dump_change_metadata(data, tmp_path, strict=False)
    assert not (tmp_path / ".openspec.yaml").exists()
    data = {"schema": "mctl-devloop", f"x-mctl-{key}": "x"}
    with pytest.raises(MetadataError, match=key):
        dump_change_metadata(data, tmp_path, strict=False)
    assert not (tmp_path / ".openspec.yaml").exists()


def test_t7_forbidden_key_in_file_rejected():
    with pytest.raises(MetadataError, match="approval"):
        load_change_metadata(FIX / "forbidden")
    assert yaml.safe_load((FIX / "forbidden" / ".openspec.yaml").read_text())
