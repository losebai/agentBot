# 构建独立 Windows 桌面包；不修改开发虚拟环境，不读取本地运行配置。
param(
    [string]$OutputDirectory = "dist/windows",
    [switch]$BuildInstaller,
    [string]$NapCatPayloadDirectory,
    [string]$IsccPath = $env:ISCC_PATH
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$previousEnvironment = $env:UV_PROJECT_ENVIRONMENT
Push-Location $repoRoot
try {
    $outputRoot = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $OutputDirectory))
    $bundle = Join-Path $outputRoot "taobot"
    # 输入必须在可清理输出之外，否则 PyInstaller 会先删除待复制的唯一载荷。
    if ($NapCatPayloadDirectory) {
        $napcatSource = (Resolve-Path -LiteralPath $NapCatPayloadDirectory).Path
        if ($napcatSource.Equals($bundle, [System.StringComparison]::OrdinalIgnoreCase) -or
            $napcatSource.StartsWith($bundle + [System.IO.Path]::DirectorySeparatorChar,
                [System.StringComparison]::OrdinalIgnoreCase) -or
            $bundle.StartsWith($napcatSource + [System.IO.Path]::DirectorySeparatorChar,
                [System.StringComparison]::OrdinalIgnoreCase)) {
            throw 'NAPCAT_PAYLOAD_OUTPUT_OVERLAP: payload source overlaps build output.'
        }
    }
    # PyInstaller 会重建输出目录，禁止覆盖任何已被用作部署目录的产物。
    if (Test-Path -LiteralPath $bundle) {
        $generatedNames = @("taobot.exe", "taobot-cli.exe", "_internal", ".env.example", "WINDOWS_README.md")
        if (Test-Path -LiteralPath (Join-Path $bundle 'runtime')) {
            $runtimeItems = @(Get-ChildItem -LiteralPath (Join-Path $bundle 'runtime') -Force)
            if ($runtimeItems.Count -ne 1 -or $runtimeItems[0].Name -ne 'napcat-node') {
                throw '输出目录含有未声明的运行资源，请指定新的构建目录。'
            }
            & uv run --no-sync python tools/prepare_napcat_payload.py (Join-Path $bundle 'runtime/napcat-node') --check
            if ($LASTEXITCODE -ne 0) { throw '输出目录中的 NapCat 资源含数据或不完整，已取消覆盖。' }
            $generatedNames += 'runtime'
        }
        $runtimeFiles = Get-ChildItem -LiteralPath $bundle -Force |
            Where-Object { $_.Name -notin $generatedNames }
        if ($runtimeFiles) {
            throw "输出目录已有运行数据，请用 -OutputDirectory 指定新的构建目录。"
        }
    }
    # 前端也使用隔离目录，避免 npm ci 删除正在开发服务中使用的依赖。
    $buildWeb = Join-Path $repoRoot "build/windows/web"
    if (Test-Path -LiteralPath $buildWeb) {
        # 清理构建副本前校验绝对路径和链接属性，避免误删源码或目录链接目标。
        $resolvedWeb = (Resolve-Path -LiteralPath $buildWeb).Path
        $buildRoot = [System.IO.Path]::GetFullPath((Join-Path $repoRoot "build/windows"))
        if (-not $resolvedWeb.StartsWith($buildRoot + [System.IO.Path]::DirectorySeparatorChar,
                [System.StringComparison]::OrdinalIgnoreCase) -or
            ((Get-Item -LiteralPath $buildWeb).Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
            throw "前端构建副本路径不安全，已取消清理。"
        }
        Remove-Item -LiteralPath $resolvedWeb -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $buildWeb | Out-Null
    Get-ChildItem -LiteralPath (Join-Path $repoRoot "web") |
        Where-Object { $_.Name -notin @("node_modules", "dist") } |
        Copy-Item -Destination $buildWeb -Recurse -Force
    & npm.cmd --prefix $buildWeb ci
    if ($LASTEXITCODE -ne 0) { throw "前端依赖安装失败" }
    & npm.cmd --prefix $buildWeb run typecheck
    if ($LASTEXITCODE -ne 0) { throw "前端类型检查失败" }
    & npm.cmd --prefix $buildWeb run build -- --outDir (Join-Path $repoRoot "src/static")
    if ($LASTEXITCODE -ne 0) { throw "前端构建失败" }

    $env:UV_PROJECT_ENVIRONMENT = Join-Path $repoRoot "build/windows/venv"
    # uv 对本地 wheel 的默认缓存键不覆盖所有源码/前端文件，强制重装当前项目。
    & uv sync --locked --no-editable --reinstall-package taobot `
        --extra desktop --group windows-build --python 3.12
    if ($LASTEXITCODE -ne 0) { throw "独立构建环境安装失败" }
    $buildPython = Join-Path $env:UV_PROJECT_ENVIRONMENT "Scripts/python.exe"
    & $buildPython -m PyInstaller --noconfirm --distpath $outputRoot `
        --workpath (Join-Path $repoRoot "build/windows/pyinstaller") tools/windows.spec
    if ($LASTEXITCODE -ne 0) { throw "Windows EXE 构建失败" }

    Copy-Item -LiteralPath ".env.example" -Destination $bundle
    Copy-Item -LiteralPath "WINDOWS_README.md" -Destination $bundle
    if ($NapCatPayloadDirectory) {
        & $buildPython tools/prepare_napcat_payload.py $napcatSource --check
        if ($LASTEXITCODE -ne 0) { throw 'NapCat 载荷校验失败。' }
        $runtimeTarget = Join-Path $bundle 'runtime'
        New-Item -ItemType Directory -Force -Path $runtimeTarget | Out-Null
        Copy-Item -LiteralPath $napcatSource -Destination (Join-Path $runtimeTarget 'napcat-node') -Recurse
    }
    # 便携包与安装器共用同一份已构建程序，版本来自已安装的发布包。
    if ($BuildInstaller) {
        $version = & $buildPython -c "from importlib.metadata import version; print(version('taobot'))"
        if ($LASTEXITCODE -ne 0) { throw "无法读取 Windows 包版本" }
        & (Join-Path $PSScriptRoot "build_installer.ps1") -BundleDirectory $bundle `
            -OutputDirectory (Join-Path $outputRoot "installer") -Version $version -IsccPath $IsccPath
    }
    Write-Host "桌面包已生成：$bundle"
    Write-Host "请分发整个 taobot 文件夹；双击 taobot.exe 打开界面。"
} finally {
    $env:UV_PROJECT_ENVIRONMENT = $previousEnvironment
    Pop-Location
}
