"""载荷必须完整且干净，路径不能逃出指定目录。"""

import json

import pytest

from napcat_fakes import make_payload


def test_payload_checks_hash_and_rejects_extra_data(tmp_path):
    """摘要错误及未声明配置均拒绝，不能用启动失败代替发布校验。"""
    from agent_bot.napcat.payload import PayloadError, load_payload

    root = make_payload(tmp_path / "runtime")
    assert load_payload(root).napcat_version == "v4.18.38"
    assert not (root / "qq").exists()
    (root / "node/napcat/napcat.mjs").write_bytes(b"modified")
    with pytest.raises(PayloadError):
        load_payload(root)
    make_payload(root)
    (root / ".env").write_text("secret", encoding="utf-8")
    with pytest.raises(PayloadError):
        load_payload(root)


def test_runtime_accepts_native_beacon_log_but_build_rejects_it(tmp_path):
    """真实原生组件会产生固定日志；运行允许保留，分发仍拒绝收集用户日志。"""
    from agent_bot.napcat.payload import PayloadError, load_payload

    root = make_payload(tmp_path / "runtime")
    (root / "node/beacon_report.log").write_text("offline native report")
    with pytest.raises(PayloadError):
        load_payload(root)
    assert load_payload(root, runtime=True).napcat_version == "v4.18.38"
    (root / "node/unexpected.log").write_text("not an approved native report")
    with pytest.raises(PayloadError):
        load_payload(root, runtime=True)


@pytest.mark.parametrize(("field", "version"), [
    ("schema_version", 2),
    ("qq_source", "installed"),
    ("runtime", "injection"),
    ("node_version", "v22.12.0"),
    ("napcat_version", "v4.18.39"),
])
def test_payload_rejects_unpinned_versions(tmp_path, field, version):
    """摘要有效也拒绝旧注入清单、其他运行方式和非固定版本。"""
    from agent_bot.napcat.payload import PayloadError, load_payload

    root = make_payload(tmp_path / "runtime")
    path = root / "manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data[field] = version
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(PayloadError):
        load_payload(root)


@pytest.mark.parametrize("name", ["qq/QQ.exe", "node/QQ.exe", "node/unexpected.exe"])
def test_payload_rejects_bundled_qq_even_when_listed_with_valid_hash(tmp_path, name):
    """载荷只带配套原生组件，不能借合法摘要混入 QQ 客户端程序。"""
    from agent_bot.napcat.payload import PayloadError, file_sha256, load_payload

    root = make_payload(tmp_path / "runtime")
    executable = root / name
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(b"not-an-executable")
    path = root / "manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["files"][name] = file_sha256(executable)
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
