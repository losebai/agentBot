# 一键安装当前 Windows 发布版；校验下载文件后交给原生安装器，保留已有配置与数据。
param([string]$BaseUrl = 'https://public.loserbai.cn/agentbot')

# 独立作用域避免 irm | iex 修改调用者的错误处理设置，也不退出调用者的终端。
& {
    param([string]$DownloadBase)
    $ErrorActionPreference = 'Stop'
    if (-not [Environment]::Is64BitOperatingSystem) {
        throw 'AgentBot requires Windows 10/11 x64.'
    }
    $DownloadBase = $DownloadBase.TrimEnd('/')
    $response = Invoke-WebRequest -Uri "$DownloadBase/latest.json" -UseBasicParsing
    $release = $response.Content | ConvertFrom-Json
    # 清单只能指定当前版本的固定安装器名，不能借文件名写入临时目录外或执行其他资源。
    if ($release.version -notmatch '^\d+\.\d+\.\d+$' -or
        $release.filename -cne "AgentBot-$($release.version)-windows-x64-Setup.exe" -or
        $release.sha256 -notmatch '^[a-fA-F0-9]{64}$') {
        throw 'Invalid AgentBot release manifest.'
    }
    $availableVersion = [version]$release.version
    Write-Host "Available AgentBot version: $availableVersion"
    # 固定 AppId 对应当前用户的安装登记；只读 DisplayVersion，不创建版本标记或改写注册表。
    $installationKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\' +
        '{379D6B51-BAED-48B4-A39D-FB1E5A424042}_is1'
    if (Test-Path -LiteralPath $installationKey) {
        $installedText = (Get-ItemProperty -LiteralPath $installationKey).DisplayVersion
        if ($installedText -notmatch '^\d+\.\d+\.\d+$') {
            throw 'Invalid installed AgentBot version. Check the Windows installation entry.'
        }
        $installedVersion = [version]$installedText
        Write-Host "Installed AgentBot version: $installedVersion"
        # 按数字分段比较，避免 0.5.10 被字符串排序判为比 0.5.9 更旧；跳过时不下载安装器。
        if ($installedVersion -eq $availableVersion) {
            Write-Host "AgentBot $installedVersion is already up to date."
            return
        }
        if ($installedVersion -gt $availableVersion) {
            Write-Host "Keeping installed version $installedVersion; available version is older."
            return
        }
        Write-Host "Updating AgentBot $installedVersion -> $availableVersion"
    } else {
        Write-Host "Installing AgentBot $availableVersion for the first time."
    }
    $temporary = Join-Path $env:TEMP ("agentbot-install-" + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $temporary | Out-Null
    $installer = Join-Path $temporary $release.filename
    try {
        Write-Host "Downloading AgentBot $($release.version)..."
        Invoke-WebRequest -Uri "$DownloadBase/$($release.filename)" -OutFile $installer -UseBasicParsing
        $actualHash = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash
        if ($actualHash -ine $release.sha256) {
            throw 'SHA256 verification failed. Installation cancelled.'
        }
        Write-Host 'Installing AgentBot for the current user...'
        # 显示安装进度，禁止自动重启系统；固定 AppId 的安装器负责原位升级并保留运行数据。
        $process = Start-Process -FilePath $installer -Wait -PassThru -ArgumentList @(
            '/SILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-', '/TASKS=desktopicon'
        )
        if ($process.ExitCode -ne 0) {
            throw "AgentBot installer failed with exit code $($process.ExitCode)."
        }
        # 安装成功后由原生安装器更新 DisplayVersion，失败时脚本不提前修改本机版本。
        Write-Host "AgentBot $availableVersion installed. Open AgentBot from the desktop or Start menu."
        Write-Host 'Microsoft Edge WebView2 Runtime is required to open the desktop window.'
    } finally {
        # 只删除本次创建的下载文件与空目录，不递归清理安装目录或其他任务的临时资源。
        if (Test-Path -LiteralPath $installer) { Remove-Item -LiteralPath $installer -Force }
        Remove-Item -LiteralPath $temporary -Force
    }
} $BaseUrl
