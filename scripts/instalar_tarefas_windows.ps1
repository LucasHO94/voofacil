<#
.SYNOPSIS
    Registra as tarefas agendadas do FareWatch no Agendador de Tarefas do Windows.

.DESCRIPTION
    Cria/atualiza tarefas que rodam o worker em modo --once (varredura) e
    --once-explore (busca ampla) nos horarios definidos, SEM precisar manter
    um processo Python ligado o tempo todo (mais robusto que o modo daemon
    do worker.py em uma maquina Windows que pode dormir/reiniciar).

    Por padrao roda em modo "somente quando o usuario estiver conectado"
    (sem senha, sem privilegio de admin). Passe -RodarDesconectado para criar
    com "executar estando o usuario conectado ou nao" (pede senha e requer
    PowerShell elevado).

.EXAMPLE
    .\scripts\instalar_tarefas_windows.ps1
    .\scripts\instalar_tarefas_windows.ps1 -RodarDesconectado
    .\scripts\instalar_tarefas_windows.ps1 -Remover
#>
param(
    [switch]$RodarDesconectado,
    [switch]$Remover
)

$ErrorActionPreference = "Stop"

$RaizProjeto = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RaizProjeto ".venv\Scripts\python.exe"
$PastaTarefas = "FareWatch"

$Tarefas = @(
    @{ Nome = "FareWatch-Varredura-0600"; Hora = "06:00"; Args = "-m farewatch.worker --once" }
    @{ Nome = "FareWatch-Varredura-1230"; Hora = "12:30"; Args = "-m farewatch.worker --once" }
    @{ Nome = "FareWatch-Varredura-2100"; Hora = "21:00"; Args = "-m farewatch.worker --once" }
    @{ Nome = "FareWatch-Explore-0500";   Hora = "05:00"; Args = "-m farewatch.worker --once-explore" }
)

if ($Remover) {
    foreach ($t in $Tarefas) {
        $existe = Get-ScheduledTask -TaskName $t.Nome -TaskPath "\$PastaTarefas\" -ErrorAction SilentlyContinue
        if ($existe) {
            Unregister-ScheduledTask -TaskName $t.Nome -TaskPath "\$PastaTarefas\" -Confirm:$false
            Write-Host "Removida: $($t.Nome)"
        }
    }
    exit 0
}

if (-not (Test-Path $Python)) {
    Write-Error "venv nao encontrado em $Python. Rode primeiro: python -m venv .venv && .venv\Scripts\pip install -e .[dev]"
}

foreach ($t in $Tarefas) {
    $trigger = New-ScheduledTaskTrigger -Daily -At $t.Hora
    $action = New-ScheduledTaskAction -Execute $Python -Argument $t.Args -WorkingDirectory $RaizProjeto
    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -DontStopOnIdleEnd `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
        -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 5)

    if ($RodarDesconectado) {
        Write-Host "Configurando '$($t.Nome)' para rodar mesmo desconectado (pedira senha da conta atual)..."
        $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Password -RunLevel Limited
        Register-ScheduledTask -TaskName $t.Nome -TaskPath "\$PastaTarefas\" `
            -Trigger $trigger -Action $action -Settings $settings -Principal $principal -Force |
            Out-Null
    } else {
        Register-ScheduledTask -TaskName $t.Nome -TaskPath "\$PastaTarefas\" `
            -Trigger $trigger -Action $action -Settings $settings -Force |
            Out-Null
    }
    Write-Host "OK: $($t.Nome) as $($t.Hora) -> python $($t.Args)"
}

Write-Host "`nPronto. Veja as tarefas em: taskschd.msc -> Biblioteca do Agendador de Tarefas -> $PastaTarefas"
Write-Host "Para remover tudo: .\scripts\instalar_tarefas_windows.ps1 -Remover"
