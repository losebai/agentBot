# 用明确列出的程序资源生成安装器；用户配置、数据库、媒体和日志不进入安装包。
param(
    [Parameter(Mandatory = $true)][string]$BundleDirectory,
    [Parameter(Mandatory = $true)][ValidatePattern('^\d+\.\d+\.\d+$')][string]$Version,
    [string]$OutputDirectory = "dist/windows/installer",
    [string]$IsccPath = $env:ISCC_PATH
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$bundle = (Resolve-Path -LiteralPath $BundleDirectory).Path
foreach ($name in @("AgentBot.exe", "agent-bot.exe", "_internal", ".env.example", "WINDOWS_README.md")) {
    if (-not (Test-Path -LiteralPath (Join-Path $bundle $name))) {
        throw "Windows 程序包不完整：缺少 $name"
    }
}

# 支持 Runner 显式配置编译器；未配置时使用 PATH 或 Inno Setup 6 的标准安装位置。
if (-not $IsccPath) {
    $command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($command) {
        $IsccPath = $command.Source
    } else {
        $IsccPath = Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6/ISCC.exe"
    }
}
if (-not (Test-Path -LiteralPath $IsccPath -PathType Leaf)) {
    throw "未找到 Inno Setup 编译器，请安装 Inno Setup 6，或设置 ISCC_PATH。"
}
$output = [System.IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Force -Path $output | Out-Null
$installer = Join-Path $output "AgentBot-$Version-windows-x64-Setup.exe"
# 编译失败不允许把同名旧产物当作新安装包；只删除明确的生成文件。
if (Test-Path -LiteralPath $installer) { Remove-Item -LiteralPath $installer -Force }
& $IsccPath "/DBundleDir=$bundle" "/DAppVersion=$Version" "/DOutputDir=$output" `
    (Join-Path $repoRoot "tools/windows-installer.iss")
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $installer -PathType Leaf)) {
    throw "Windows 安装器编译失败"
}
$hash = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant()
"$hash  $([System.IO.Path]::GetFileName($installer))" |
    Set-Content -LiteralPath (Join-Path $output "SHA256SUMS.txt") -Encoding ascii
Write-Host "Windows 安装包已生成：$installer"
