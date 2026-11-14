param([string] $ConfigPath = (Join-Path $PSScriptRoot "config.json"))
$ErrorActionPreference = "Stop"
$Config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
$PortalUrl = ([string]$Config.broker_url).TrimEnd('/') + "/employee"
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$notify = New-Object System.Windows.Forms.NotifyIcon
$notify.Icon = [System.Drawing.SystemIcons]::Application
$notify.Text = "CWS Codex · $PortalUrl"
$menu = New-Object System.Windows.Forms.ContextMenuStrip
$open = $menu.Items.Add("打开员工账号查询")
$open.Add_Click({ Start-Process -FilePath $PortalUrl })
$select = $menu.Items.Add("重新选择账号")
$select.Add_Click({ Start-Process -FilePath (Join-Path $PSScriptRoot "Launch-CwsCodex.cmd") })
$menu.Items.Add("退出托盘") | Out-Null
$menu.Items[2].Add_Click({ $notify.Visible = $false; [System.Windows.Forms.Application]::Exit() })
$notify.ContextMenuStrip = $menu
$notify.Add_DoubleClick({ Start-Process -FilePath $PortalUrl })
$notify.Visible = $true
[System.Windows.Forms.Application]::Run()
$notify.Dispose()
