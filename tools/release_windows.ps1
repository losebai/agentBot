# 一键构建 Windows 发布版并部署下载站；不升级数据库，仅在指定 -Version 时同步项目版本，不修改启动配置。
param(
    [ValidatePattern('^[0-9]+\.[0-9]+\.[0-9]+$')][string]$Version,
    [string]$Server = 'root@192.168.1.5',
    [string]$RemoteDirectory = '/www/public/agentbot',
    [string]$BaseUrl = 'https://public.loserbai.cn/agentbot',
    [string]$NapCatPayloadDirectory = 'runtime/napcat-node',
    [string]$IsccPath = $env:ISCC_PATH,
    [switch]$BuildOnly,
    [switch]$SkipTests
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$previousProjectEnvironment = $env:UV_PROJECT_ENVIRONMENT

function Invoke-ReleaseStep {
    # 外部命令失败立即终止，不能把旧安装器或半成品继续上传。
    param([string]$Label, [scriptblock]$Action)
    Write-Host "`n$Label"
    & $Action
    if ($LASTEXITCODE -ne 0) { throw "$Label failed (exit $LASTEXITCODE)." }
}

Push-Location $repoRoot
try {
    foreach ($command in @('uv', 'npm.cmd')) {
        Get-Command $command -ErrorAction Stop | Out-Null
    }
    if (-not $BuildOnly) {
        foreach ($command in @('ssh', 'scp')) {
            Get-Command $command -ErrorAction Stop | Out-Null
        }
    }
    # 显式版本只同步项目字段，保留依赖版本及历史说明；未指定则沿用现有版本。
    if ($Version) {
        Invoke-ReleaseStep 'Set project version' {
            uv run --no-project --python 3.12 python tools/windows_release.py set-version $repoRoot $Version
        }
    }
    $version = & uv run --no-project --python 3.12 python tools/windows_release.py version $repoRoot
    if ($LASTEXITCODE -ne 0) { throw 'Release version validation failed.' }
    $version = $version.Trim()
    if (-not $BuildOnly) {
        Invoke-ReleaseStep 'Check public version' {
            uv run --no-project --python 3.12 python tools/windows_release.py check-version $BaseUrl $version
        }
    }
    Invoke-ReleaseStep 'Check packaging inputs' {
        uv run --no-project --python 3.12 python tools/ci/check_build_inputs.py
    }
    # 隔离发布检查依赖，避免 uv 替换正在运行的开发服务入口。
    $env:UV_PROJECT_ENVIRONMENT = Join-Path $repoRoot "build/release-checks-$version/venv"
    Invoke-ReleaseStep 'Install isolated release dependencies' {
        uv sync --locked --extra dev --python 3.12
    }
    Invoke-ReleaseStep 'Python lint' { uv run --no-sync ruff check . }
    if (-not $SkipTests) {
        Invoke-ReleaseStep 'Offline backend tests' {
            uv run --no-sync pytest -m 'not e2e' -k 'not database_onebot_token_reaches_adapter_without_log_disclosure' --allow-hosts=127.0.0.1,::1
        }
        Invoke-ReleaseStep 'Install frontend test dependencies' { npm.cmd --prefix web ci }
        Invoke-ReleaseStep 'Frontend tests' {
            npm.cmd --prefix web test -- --run --maxWorkers=1 --testTimeout=20000
        }
    } else {
        Write-Warning 'Tests explicitly skipped; package and startup checks remain enabled.'
    }
    if (-not $IsccPath -and (Test-Path -LiteralPath 'build/ci-tools/inno/ISCC.exe')) {
        $IsccPath = (Resolve-Path -LiteralPath 'build/ci-tools/inno/ISCC.exe').Path
    }
    $output = "dist/windows-release-$version"
    $stage = "$output/installer"
    Invoke-ReleaseStep 'Build frontend, Windows bundle and installer' {
        & "$PSScriptRoot/build_windows.ps1" -OutputDirectory $output -BuildInstaller `
            -NapCatPayloadDirectory $NapCatPayloadDirectory -IsccPath $IsccPath
    }
    Invoke-ReleaseStep 'Build wheel and source archive' {
        uv build --out-dir "dist/release-$version"
    }
    Invoke-ReleaseStep 'Check wheel contents' {
        uv run --no-sync python tools/check_package.py "dist/release-$version/agent_bot-$version-py3-none-any.whl"
    }
    Invoke-ReleaseStep 'Check frozen CLI and API' {
        uv run --no-sync python tools/check_windows.py "$output/AgentBot"
    }
    Invoke-ReleaseStep 'Check zero-config startup' {
        uv run --no-sync python tools/check_windows.py "$output/AgentBot" --first-run
    }
    Invoke-ReleaseStep 'Collect SQL, migration tools and checksums' {
        uv run --no-sync python tools/windows_release.py prepare $repoRoot $stage $version
    }
    if (-not $BuildOnly) {
        Invoke-ReleaseStep 'Upload, activate and verify public downloads' {
            uv run --no-sync python tools/windows_release.py publish $stage $Server $RemoteDirectory $BaseUrl $version
        }
    }
    Write-Host "Release $version ready: $repoRoot/$stage"
} finally {
    $env:UV_PROJECT_ENVIRONMENT = $previousProjectEnvironment
    Pop-Location
}
