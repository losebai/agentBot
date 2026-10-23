"""收集 Windows 发布资源、校验上传并原子切换清单，不操作业务数据库。"""

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen
from zipfile import ZIP_DEFLATED, ZipFile


def sha256(path: Path) -> str:
    """流式校验安装器，避免把大文件一次读入内存。"""
    with path.open('rb') as stream:
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
        return digest.hexdigest()


def version_tuple(value: str) -> tuple[int, ...]:
    """只接受项目安装器支持的三段数字版本。"""
    if not re.fullmatch(r'\d+\.\d+\.\d+', value):
        raise ValueError(f'Invalid release version: {value}')
    return tuple(map(int, value.split('.')))


def read_json(path: Path) -> dict:
    """兼容 Windows 生成的清单编码。"""
    return json.loads(path.read_text(encoding='utf-8-sig'))


def sums(stage: Path) -> dict[str, str]:
    """校验文件白名单，禁止清单把文件移动到发布目录之外。"""
    result = {}
    for line in (stage / 'SHA256SUMS.txt').read_text(encoding='ascii').splitlines():
        digest, name = line.split('  ', 1)
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', name) or name in {'.', '..'}:
            raise ValueError('Invalid release filename')
        if not re.fullmatch(r'[0-9a-f]{64}', digest) or sha256(stage / name) != digest:
            raise ValueError(f'SHA256 mismatch: {name}')
        result[name] = digest
    return result


def prepare(root: Path, stage: Path, version: str) -> None:
    """SQL 与迁移工具显式入包，Git 忽略不影响发布资源。"""
    guide = root / 'sql' / f'UPGRADE-{version}.md'
    if not guide.is_file():
        raise ValueError(f'Missing upgrade guide: {guide}')
    files = sorted(p for p in (root / 'sql').rglob('*')
                   if p.is_file() and p.suffix in {'.sql', '.md'})
    files += sorted((root / 'tools').glob('migrate_*.py'))
    archive = stage / f'AgentBot-{version}-sql.zip'
    with ZipFile(archive, 'w', ZIP_DEFLATED) as output:
        for path in files:
            output.write(path, path.relative_to(root).as_posix())
    wheel = root / 'dist' / f'release-{version}' / f'agent_bot-{version}-py3-none-any.whl'
    shutil.copy2(wheel, stage)
    for path in (root / 'sql').glob('UPGRADE*.md'):
        shutil.copy2(path, stage)
    release = read_json(stage / 'latest.json')
    installer = f'AgentBot-{version}-windows-x64-Setup.exe'
    if release != {'version': version, 'filename': installer, 'sha256': sha256(stage / installer)}:
        raise ValueError('Installer manifest does not match the release')
    names = [installer, archive.name, wheel.name, 'install.ps1', 'install.bat']
    names += sorted(p.name for p in stage.glob('UPGRADE*.md'))
    content = ''.join(f'{sha256(stage / name)}  {name}\n' for name in names)
    (stage / 'SHA256SUMS.txt').write_text(content, encoding='ascii')
    shutil.copy2(stage / 'SHA256SUMS.txt', stage / f'SHA256SUMS-{version}.txt')
    shutil.copy2(stage / 'latest.json', stage / f'latest-{version}.json')


def activate(root: Path, stage: Path, version: str) -> None:
    """全部校验先于公开目录修改，安装器等资源就绪后最后原子替换 latest。"""
    release = read_json(stage / 'latest.json')
    previous = read_json(root / 'latest.json')
    if (release['version'] != version
            or version_tuple(version) <= version_tuple(previous['version'])):
        raise ValueError('Release version must be newer than the public version')
    files = sums(stage)
    installer = f'AgentBot-{version}-windows-x64-Setup.exe'
    if release.get('filename') != installer or files.get(installer) != release.get('sha256'):
        raise ValueError('Invalid installer manifest')
    for name, digest in files.items():
        if version in name and (root / name).exists() and sha256(root / name) != digest:
            raise ValueError(f'Immutable release already exists: {name}')
    metadata = ['SHA256SUMS.txt', f'SHA256SUMS-{version}.txt', f'latest-{version}.json']
    for name in metadata:
        if not (stage / name).is_file():
            raise ValueError(f'Missing release metadata: {name}')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')
    shutil.copy2(root / 'latest.json', root / f'latest-backup-{stamp}.json')
    if (root / 'SHA256SUMS.txt').exists():
        shutil.copy2(root / 'SHA256SUMS.txt', root / f'SHA256SUMS-backup-{stamp}.txt')
    for name in list(files) + metadata:
        (stage / name).chmod(0o644)
        os.replace(stage / name, root / name)
    (stage / 'latest.json').chmod(0o644)
    os.replace(stage / 'latest.json', root / 'latest.json')
    print(f'Published {version}; previous release {previous["version"]} preserved.')


def check_version(base_url: str, version: str) -> None:
    """构建前拒绝同版本覆盖或降级；服务器切换时再次核对防止过时发布。"""
    with urlopen(base_url.rstrip('/') + '/latest.json', timeout=60) as response:
        current = json.load(response)
    if version_tuple(version) <= version_tuple(current['version']):
        raise ValueError(f'请先更新项目版本；服务器当前版本为 {current["version"]}')


def publish(stage: Path, server: str, remote: str, base_url: str, version: str) -> None:
    """上传独立暂存目录并核验后切换；SSH 只使用既有认证，不存储密码。"""
    if not re.fullmatch(r'[A-Za-z0-9_.@-]+', server) or server.startswith('-'):
        raise ValueError('Invalid SSH server')
    if not re.fullmatch(r'/[A-Za-z0-9_/.-]+', remote) or '..' in remote.split('/'):
        raise ValueError('Invalid remote directory')
    check_version(base_url, version)
    target = remote.rstrip('/') + f'/.release-{version}-{uuid.uuid4().hex}'
    subprocess.run(['ssh', '-o', 'BatchMode=yes', server, f'mkdir -p {shlex.quote(target)}'],
                   check=True)
    files = [*sums(stage), 'SHA256SUMS.txt', f'SHA256SUMS-{version}.txt',
             'latest.json', f'latest-{version}.json']
    subprocess.run(['scp', '-q', *[str(stage / name) for name in files], f'{server}:{target}/'],
                   check=True)
    command = shlex.join(['python3', '-', 'activate', remote, target, version])
    subprocess.run(['ssh', '-o', 'BatchMode=yes', server, command],
                   input=Path(__file__).read_bytes(), check=True)
    verify(stage, base_url)


def verify(stage: Path, base_url: str) -> None:
    """完整下载公网资源核对 SHA256，校验失败返回非零，不宣称发布成功。"""
    base = base_url.rstrip('/') + '/'
    with urlopen(base + 'latest.json', timeout=60) as response:
        if json.load(response) != read_json(stage / 'latest.json'):
            raise ValueError('Public latest.json mismatch')
    for name, expected in sums(stage).items():
        digest = hashlib.sha256()
        with urlopen(base + name, timeout=60) as response:
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise ValueError(f'Public SHA256 mismatch: {name}')
        print(f'Public SHA256 verified: {name}', flush=True)


def project_version(root: Path) -> str:
    """本机用 Python 3.12 核对版本，服务器切换无需导入 tomllib。"""
    import tomllib

    version = tomllib.loads((root / 'pyproject.toml').read_text('utf-8'))['project']['version']
    version_tuple(version)
    if not re.search(r'__version__ = "' + re.escape(version) + '"',
                     (root / 'src/agent_bot/__init__.py').read_text('utf-8')):
        raise ValueError('Python source version mismatch')
    for name in ['web/package.json', 'web/package-lock.json']:
        if read_json(root / name)['version'] != version:
            raise ValueError(f'Version mismatch: {name}')
    lock = tomllib.loads((root / 'uv.lock').read_text('utf-8'))
    if next(p['version'] for p in lock['package'] if p['name'] == 'agent-bot') != version:
        raise ValueError('uv.lock project version mismatch')
    if not (root / 'sql' / f'UPGRADE-{version}.md').is_file():
        raise ValueError(f'Missing sql/UPGRADE-{version}.md')
    return version


def main() -> None:
    """同一标准库入口可在本机构建环境及 Linux 下载服务器运行。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['version', 'prepare', 'activate', 'publish',
                                          'check-version'])
    parser.add_argument('arguments', nargs='+')
    args = parser.parse_args()
    if args.command == 'version':
        print(project_version(Path(args.arguments[0])))
    elif args.command == 'prepare':
        prepare(Path(args.arguments[0]), Path(args.arguments[1]), args.arguments[2])
    elif args.command == 'activate':
        activate(Path(args.arguments[0]), Path(args.arguments[1]), args.arguments[2])
    elif args.command == 'check-version':
        check_version(*args.arguments)
    else:
        publish(Path(args.arguments[0]), *args.arguments[1:])


if __name__ == '__main__':
    main()
