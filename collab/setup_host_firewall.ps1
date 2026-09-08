# setup_host_firewall.ps1
# 中枢主机 Windows 防火墙 — 放行协作端口
# 用途: 新队友接入时，中枢管理员以管理员身份运行此脚本
# 用法: 右键 → 以管理员身份运行 PowerShell → .\setup_host_firewall.ps1

Write-Host ""
Write-Host "╔══════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "║   Claude 协作平台 — 防火墙配置      ║" -ForegroundColor Cyan
Write-Host "╚══════════════════════════════════════╝" -ForegroundColor Cyan
Write-Host ""

$ports = @(
    @{Port=8022; Name="Claude Collab SSH"},
    @{Port=80;   Name="Claude Collab Nginx (HTTP)"},
    @{Port=9000; Name="Claude Collab App Dev"},
    @{Port=8080; Name="Claude Collab Nginx (Alt)"}
)

foreach ($p in $ports) {
    $port = $p.Port
    $name = $p.Name
    $ruleName = "ClaudeCollab_${port}_${name}"

    $existing = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "  [$port] $name — 已存在，跳过" -ForegroundColor Yellow
    } else {
        New-NetFirewallRule -DisplayName $ruleName `
            -Direction Inbound `
            -Protocol TCP `
            -LocalPort $port `
            -Action Allow `
            -Profile Private,Domain `
            -Description "Claude 协作平台 — $name" `
            | Out-Null
        Write-Host "  [$port] $name — ✅ 已放行" -ForegroundColor Green
    }
}

Write-Host ""
Write-Host "═══ 防火墙配置完成 ═══" -ForegroundColor Cyan
Write-Host ""
Write-Host "当前入站规则中的 Claude 协作端口:" -ForegroundColor White
Get-NetFirewallRule -DisplayName "ClaudeCollab_*" -ErrorAction SilentlyContinue |
    Select-Object DisplayName, Enabled, Direction, Action |
    Format-Table -AutoSize
Write-Host ""
Write-Host "提示: 确保你的网络配置文件是 '专用' (Private) 而非 '公用' (Public)" -ForegroundColor Yellow
Write-Host "查看: Get-NetConnectionProfile" -ForegroundColor Gray
