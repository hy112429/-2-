# 需要人工操作时播放提示音 + 可选气泡通知
# 用法: powershell -NoProfile -ExecutionPolicy Bypass -File tools\alert-need-action.ps1 [-Message "请点预览候选"] [-Beeps 3]
param(
  [string]$Message = "需要你操作一下",
  [int]$Beeps = 3,
  [ValidateSet("Asterisk","Exclamation","Hand","Question","Beep")]
  [string]$Sound = "Exclamation"
)

Add-Type -AssemblyName System.Windows.Forms | Out-Null
Add-Type -AssemblyName System.Drawing | Out-Null

# 系统提示音（重复几次更醒目）
$soundMap = @{
  Asterisk = [System.Media.SystemSounds]::Asterisk
  Exclamation = [System.Media.SystemSounds]::Exclamation
  Hand = [System.Media.SystemSounds]::Hand
  Question = [System.Media.SystemSounds]::Question
  Beep = [System.Media.SystemSounds]::Beep
}
$s = $soundMap[$Sound]
for ($i = 0; $i -lt [Math]::Max(1, $Beeps); $i++) {
  $s.Play()
  Start-Sleep -Milliseconds 450
}

# 托盘气泡（约 5 秒）
try {
  $ni = New-Object System.Windows.Forms.NotifyIcon
  $ni.Icon = [System.Drawing.SystemIcons]::Information
  $ni.Visible = $true
  $ni.BalloonTipTitle = "小g 需要你"
  $ni.BalloonTipText = $Message
  $ni.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Info
  $ni.ShowBalloonTip(5000)
  Start-Sleep -Seconds 5
  $ni.Visible = $false
  $ni.Dispose()
} catch {
  # 气泡失败不影响提示音
}

Write-Output ("ALERT_OK message=" + $Message)
