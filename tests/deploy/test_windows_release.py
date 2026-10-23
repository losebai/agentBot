"""隔离验证发布切换：错误产物不能替换公开清单，旧安装器必须保留。"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from zipfile import ZipFile

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'tools/windows_release.py'


@pytest.fixture
def release():
    """加载标准库发布工具，不连接服务器。"""
    spec = importlib.util.spec_from_file_location('windows_release', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def staged(tmp_path, release):
    """准备独立旧站点与新发布目录，内容均为离线替身。"""
    root = tmp_path / 'public'
    root.mkdir()
    (root / 'latest.json').write_text(json.dumps({'version': '0.5.4'}))
    (root / 'old.exe').write_bytes(b'old')
    stage = root / '.release-test'
    stage.mkdir()
    name = 'AgentBot-0.5.5-windows-x64-Setup.exe'
    (stage / name).write_bytes(b'new')
    digest = release.sha256(stage / name)
    (stage / 'latest.json').write_text(json.dumps({
        'version': '0.5.5', 'filename': name, 'sha256': digest,
    }))
    (stage / 'SHA256SUMS.txt').write_text(f'{digest}  {name}\n')
    (stage / 'SHA256SUMS-0.5.5.txt').write_bytes((stage / 'SHA256SUMS.txt').read_bytes())
    (stage / 'latest-0.5.5.json').write_bytes((stage / 'latest.json').read_bytes())
    return root, stage, name


def test_activation_preserves_old_release_and_switches_manifest_last(tmp_path, release):
    """新产物核验成功后切换，旧文件与旧清单备份可用于回退。"""
    root, stage, name = staged(tmp_path, release)
    release.activate(root, stage, '0.5.5')
    assert (root / name).read_bytes() == b'new'
    assert (root / 'old.exe').read_bytes() == b'old'
    assert json.loads((root / 'latest.json').read_text())['version'] == '0.5.5'
    assert json.loads(next(root.glob('latest-backup-*.json')).read_text())['version'] == '0.5.4'


@pytest.mark.parametrize('damage', ['hash', 'version', 'collision', 'manifest_hash'])
def test_activation_rejects_bad_release_without_public_changes(tmp_path, release, damage):
    """校验失败、非递增版本或同名产物冲突均不能修改站点。"""
    root, stage, name = staged(tmp_path, release)
    if damage == 'hash':
        (stage / name).write_bytes(b'corrupt')
    elif damage == 'version':
        (root / 'latest.json').write_text(json.dumps({'version': '0.5.5'}))
    elif damage == 'collision':
        (root / name).write_bytes(b'different')
    else:
        data = json.loads((stage / 'latest.json').read_text())
        data['sha256'] = '0' * 64
        (stage / 'latest.json').write_text(json.dumps(data))
    before = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
    with pytest.raises(ValueError):
        release.activate(root, stage, '0.5.5')
    assert {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()} == before


def test_prepare_includes_ignored_sql_and_migration_tools_only(tmp_path, release):
    """日期增量随包交付，启动密钥与数据库不能进入 SQL 归档。"""
    root = tmp_path / 'source'
    stage = tmp_path / 'installer'
    stage.mkdir()
    for name in ['sql', 'tools', 'dist/release-0.5.5']:
        (root / name).mkdir(parents=True)
    for name in ['agent_bot_init.sql', 'agent_bot_init.sqlite.sql',
                 '2026-10-20-model-identity.sql', 'UPGRADE-0.5.5.md']:
        (root / 'sql' / name).write_text('-- fixture')
    (root / 'sql/private.db').write_bytes(b'private')
    (root / '.env').write_text('SECRET=private')
    (root / 'tools/migrate_model_identity.py').write_text('# fixture')
    (root / 'dist/release-0.5.5/agent_bot-0.5.5-py3-none-any.whl').write_bytes(b'wheel')
    name = 'AgentBot-0.5.5-windows-x64-Setup.exe'
    (stage / name).write_bytes(b'installer')
    (stage / 'latest.json').write_text(json.dumps({
        'version': '0.5.5', 'filename': name, 'sha256': release.sha256(stage / name),
    }))
    for script in ['install.ps1', 'install.bat']:
        (stage / script).write_text('fixture')
    release.prepare(root, stage, '0.5.5')
    with ZipFile(stage / 'AgentBot-0.5.5-sql.zip') as archive:
        assert 'sql/2026-10-20-model-identity.sql' in archive.namelist()
        assert 'tools/migrate_model_identity.py' in archive.namelist()
        assert archive.read('sql/agent_bot_init.sqlite.sql') == b'-- fixture'
        assert not any(name.endswith(('.env', '.db')) for name in archive.namelist())
    assert name in release.sums(stage)


@pytest.mark.parametrize('corrupt', [False, True])
def test_public_download_verification_checks_actual_bytes(tmp_path, release, corrupt):
    """真实回环 HTTP 下载必须核对正文散列，不能仅凭 HTTP 200 报告成功。"""
    _, stage, name = staged(tmp_path, release)
    public = tmp_path / 'http'
    shutil.copytree(stage, public)
    if corrupt:
        (public / name).write_bytes(b'corrupt')
    server = ThreadingHTTPServer(('127.0.0.1', 0),
                                partial(SimpleHTTPRequestHandler, directory=str(public)))
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        url = f'http://127.0.0.1:{server.server_port}'
        if corrupt:
            with pytest.raises(ValueError, match='Public SHA256 mismatch'):
                release.verify(stage, url)
        else:
            release.verify(stage, url)
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


@pytest.mark.parametrize('valid', [True, False])
def test_set_version_preserves_dependencies_and_history(tmp_path, release, valid):
    """定向改项目与固定断言；非法版本不写文件，依赖和历史说明保持原样。"""
    fixtures = {
        'pyproject.toml': '[project]\nname = "agent-bot"\nversion = "0.5.5"\n',
        'uv.lock': '[[package]]\nname = "agent-bot"\nversion = "0.5.5"\n'
                   '[[package]]\nname = "other"\nversion = "0.5.5"\n',
        'src/agent_bot/__init__.py': '__version__ = "0.5.5"\n',
        'web/package.json': '{"version":"0.5.5"}',
        'web/package-lock.json': '{"version":"0.5.5","packages":'
                                 '{"":{"version":"0.5.5"},'
                                 '"node_modules/other":{"version":"0.5.5"}}}',
        'tests/agent/test_release.py': 'assert actual == "0.5.5"\n',
        'tests/media/test_release.py': 'assert actual == "0.5.5"\n',
        'sql/UPGRADE-0.5.6.md': 'upgrade guide',
        'sql/UPGRADE-0.5.5.md': 'historical version 0.5.5',
    }
    for name, content in fixtures.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
    if not valid:
        with pytest.raises(ValueError):
            release.set_version(tmp_path, '0.5.6;bad')
        assert all((tmp_path / name).read_text('utf-8') == value
                   for name, value in fixtures.items())
        return
    release.set_version(tmp_path, '0.5.6')
    assert release.project_version(tmp_path) == '0.5.6'
    lock = release.read_json(tmp_path / 'web/package-lock.json')
    assert lock['packages']['']['version'] == '0.5.6'
    assert lock['packages']['node_modules/other']['version'] == '0.5.5'
    assert 'name = "other"\nversion = "0.5.5"' in (tmp_path / 'uv.lock').read_text()
    assert (tmp_path / 'tests/agent/test_release.py').read_text() == 'assert actual == "0.5.6"\n'
    assert (tmp_path / 'sql/UPGRADE-0.5.5.md').read_text() == fixtures['sql/UPGRADE-0.5.5.md']


@pytest.mark.skipif(sys.platform != 'win32', reason='需要 Windows PowerShell')
@pytest.mark.parametrize('mode', ['publish', 'build_only', 'package_failure', 'explicit_version'])
def test_powershell_orchestration_stops_before_publish_on_failure(tmp_path, mode):
    """真实执行入口但替代构建及网络命令，核对仅构建边界和失败阻断。"""
    tools = tmp_path / 'tools'
    tools.mkdir()
    shutil.copy2(SCRIPT.with_name('release_windows.ps1'), tools)
    (tools / 'build_windows.ps1').write_text('$global:LASTEXITCODE=0', encoding='utf-8')
    trace = tmp_path / 'trace.txt'
    harness = tmp_path / 'harness.ps1'
    harness.write_text(r'''
$ErrorActionPreference='Stop'
function uv {
    Add-Content -LiteralPath $env:RELEASE_TRACE -Value ($args -join '|')
    $global:LASTEXITCODE=0
    if ($args -contains 'version') { return '0.5.6' }
    if ($env:RELEASE_MODE -eq 'package_failure' -and $args -contains 'tools/check_package.py') {
        $global:LASTEXITCODE=1
    }
}
function npm.cmd { $global:LASTEXITCODE=0 }
function ssh { throw 'Unexpected direct SSH invocation' }
function scp { throw 'Unexpected direct SCP invocation' }
if ($env:RELEASE_MODE -eq 'build_only') {
    & "$PSScriptRoot/tools/release_windows.ps1" -BuildOnly
} elseif ($env:RELEASE_MODE -eq 'explicit_version') {
    & "$PSScriptRoot/tools/release_windows.ps1" -Version 0.5.6
} else {
    & "$PSScriptRoot/tools/release_windows.ps1"
}
''', encoding='utf-8')
    result = subprocess.run(
        ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(harness)],
        env={**os.environ, 'RELEASE_TRACE': str(trace), 'RELEASE_MODE': mode},
        capture_output=True, timeout=30,
    )
    calls = trace.read_text(encoding='utf-8-sig')
    if mode == 'package_failure':
        assert result.returncode != 0
        assert '|prepare|' not in calls and '|publish|' not in calls
    else:
        assert result.returncode == 0, result.stderr.decode(errors='replace')
        assert '|prepare|' in calls
        assert ('|publish|' in calls) == (mode != 'build_only')
        assert ('|check-version|' in calls) == (mode != 'build_only')
        assert ('|set-version|' in calls) == (mode == 'explicit_version')


def test_missing_upgrade_guide_rejects_before_writing(tmp_path, release):
    """未知升级影响不能由脚本编造，缺少说明时保持工作区不变。"""
    with pytest.raises(ValueError, match=r'UPGRADE-0\.5\.6\.md'):
        release.set_version(tmp_path, '0.5.6')
    assert not list(tmp_path.iterdir())
