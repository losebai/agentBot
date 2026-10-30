"""载荷必须完整且干净，路径不能逃出指定目录。"""

import json

import pytest

from napcat_fakes import make_payload


def test_payload_checks_hash_and_rejects_extra_data(tmp_path):
    """摘要错误及未声明配置均拒绝，不能用启动失败代替发布校验。"""
    from agent_bot.napcat.payload import VERSION, PayloadError, load_payload

    root = make_payload(tmp_path / "runtime")
    assert load_payload(root).napcat_version == VERSION
    assert not (root / "qq").exists()
    (root / "shell/napcat.mjs").write_bytes(b"modified")
    with pytest.raises(PayloadError):
        load_payload(root)
    make_payload(root)
    (root / ".env").write_text("secret", encoding="utf-8")
    with pytest.raises(PayloadError):
        load_payload(root)


def test_quarantine_moves_runtime_residue_before_validation(tmp_path):
    """QQ 运行残留被隔离保留而非删除；隔离后整体校验恢复通过。"""
    from agent_bot.napcat.payload import quarantine_runtime_files, load_payload

    root = make_payload(tmp_path / "runtime")
    residue = tmp_path / "data/napcat/residue"
    (root / "shell/guild1.db").write_bytes(b"runtime-data")
    (root / "shell/guild1.db-shm").write_bytes(b"runtime-shm")

    moved = quarantine_runtime_files(root, residue)

    assert sorted(moved) == ["shell/guild1.db", "shell/guild1.db-shm"]
    assert not (root / "shell/guild1.db").exists()
    assert list(residue.rglob("guild1.db"))[0].read_bytes() == b"runtime-data"
    load_payload(root)


def test_quarantine_keeps_files_without_usable_manifest(tmp_path):
    """清单缺失或损坏时不移动任何文件，交由整体校验明确报错。"""
    from agent_bot.napcat.payload import PayloadError, load_payload, quarantine_runtime_files

    root = make_payload(tmp_path / "runtime")
    (root / "shell/stray.db").write_bytes(b"stray")
    (root / "manifest.json").write_text("{broken", encoding="utf-8")

    assert quarantine_runtime_files(root, tmp_path / "residue") == []
    assert (root / "shell/stray.db").exists()
    with pytest.raises(PayloadError):
        load_payload(root)


def test_quarantine_keeps_declared_files_untouched(tmp_path):
    """声明清单内的分发文件与 manifest 本体不参与隔离。"""
    from agent_bot.napcat.payload import quarantine_runtime_files

    root = make_payload(tmp_path / "runtime")
    before = (root / "shell/napcat.mjs").read_bytes()

    assert quarantine_runtime_files(root, tmp_path / "residue") == []
    assert (root / "shell/napcat.mjs").read_bytes() == before
    assert (root / "manifest.json").exists()
    assert not (tmp_path / "residue").exists()


@pytest.mark.parametrize(("field", "version"), [
    ("schema_version", 1),
    ("qq_source", "bundled"),
    ("napcat_version", "v4.18.39"),
])
def test_payload_rejects_unpinned_versions(tmp_path, field, version):
    """摘要有效也拒绝旧清单、捆绑 QQ 模式和非固定 Shell 版本。"""
    from agent_bot.napcat.payload import PayloadError, load_payload

    root = make_payload(tmp_path / "runtime")
    path = root / "manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data[field] = version
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(PayloadError):
        load_payload(root)


def test_payload_rejects_bundled_qq_even_when_listed_with_valid_hash(tmp_path):
    """载荷只能分发 Shell，不能借合法摘要混入 QQ 程序。"""
    from agent_bot.napcat.payload import PayloadError, file_sha256, load_payload

    root = make_payload(tmp_path / "runtime")
    executable = root / "qq/QQ.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"not-an-executable")
    path = root / "manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["files"]["qq/QQ.exe"] = file_sha256(executable)
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(PayloadError):
        load_payload(root)


@pytest.mark.parametrize("name", ["../escape.exe", "C:/escape.exe", "shell\\escape.exe"])
def test_payload_rejects_escape_paths(tmp_path, name):
    """Windows 盘符和反斜线同样不能绕过相对路径约束。"""
    from agent_bot.napcat.payload import PayloadError, load_payload

    root = make_payload(tmp_path / "runtime")
    path = root / "manifest.json"
    data = json.loads(path.read_text())
    data["files"][name] = "0" * 64
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(PayloadError):
        load_payload(root)
