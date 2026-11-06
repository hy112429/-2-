# 只读检查：在客户 Windows Server 的 PowerShell 中运行，不安装角色或更改网络。
$ErrorActionPreference = 'Stop'

$os = Get-CimInstance -ClassName Win32_OperatingSystem
$computer = Get-CimInstance -ClassName Win32_ComputerSystem
$processor = Get-CimInstance -ClassName Win32_Processor | Select-Object -First 1
$hyperVCommand = Get-Command Get-WindowsFeature -ErrorAction SilentlyContinue
$hyperV = if ($hyperVCommand) { Get-WindowsFeature -Name Hyper-V } else { $null }
$dataDrive = Get-PSDrive -Name D -ErrorAction SilentlyContinue

[pscustomobject]@{
    ServerName = $env:COMPUTERNAME
    WindowsVersion = $os.Caption
    TotalMemoryGB = [math]::Round($computer.TotalPhysicalMemory / 1GB, 1)
    FreeMemoryGB = [math]::Round(($os.FreePhysicalMemory * 1KB) / 1GB, 1)
    DDriveFreeGB = if ($dataDrive) { [math]::Round($dataDrive.Free / 1GB, 1) } else { '无 D 盘' }
    HyperVRoleInstalled = if ($hyperV) { [bool]$hyperV.Installed } else { '无法读取角色状态' }
    HypervisorPresent = $computer.HypervisorPresent
    FirmwareVirtualizationEnabled = $processor.VirtualizationFirmwareEnabled
    SecondLevelAddressTranslation = $processor.SecondLevelAddressTranslationExtensions
    VmMonitorModeExtensions = $processor.VMMonitorModeExtensions
} | Format-List

Write-Host '以上是主机状态，不能单凭此输出判定可安装 Hyper-V；还需核对现有业务、虚拟化授权和维护窗口。'
