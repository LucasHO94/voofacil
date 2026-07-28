<#
.SYNOPSIS
    Registra a tarefa agendada do VooFacil no Agendador de Tarefas do Windows.

.DESCRIPTION
    Cria/atualiza UMA UNICA tarefa "VooFacil-Poll" que roda o worker em modo
    --auto (python -m voofacil.worker --auto) a cada
    settings.agendamento_poll_minutos minutos, indefinidamente. E o mesmo
    mecanismo usado na nuvem (GitHub Actions, ver .github/workflows/worker.yml):
    a cada execucao o worker consulta o banco (HorarioAgendado) e decide
    sozinho, via deve_rodar_agora(), se ha uma varredura e/ou um explore para
    rodar de verdade agora.

    Os horarios de varredura/explore NAO ficam mais neste script nem no
    Agendador - sao editaveis DIRETO NO PAINEL (aba Agendamento). O intervalo
    do poll (a cada quantos minutos a tarefa acorda para checar o banco) e
    lido em runtime de voofacil/config.py (settings.agendamento_poll_minutos,
    configuravel via .env) atraves do Python do venv do projeto.

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
$PastaTarefas = "VooFacil"
$NomeTarefa = "VooFacil-Poll"

if (-not (Test-Path $Python)) {
    Write-Error "venv nao encontrado em $Python. Rode primeiro: python -m venv .venv && .venv\Scripts\pip install -e .[dev]"
}

function Obter-PollMinutos {
    <#
        Chama o Python do venv do projeto para ler
        settings.agendamento_poll_minutos em runtime - e isso que torna o
        intervalo do poll dinamico: muda o .env, roda o script de novo, sem
        precisar editar este arquivo.
    #>
    $codigoPython = "from voofacil.config import settings; print(settings.agendamento_poll_minutos)"
    $saida = & $Python -c $codigoPython
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($saida)) {
        Write-Error "Falha ao ler settings.agendamento_poll_minutos via Python (venv em $Python). Confira o .env / voofacil/config.py."
    }
    return [int]$saida.Trim()
}

if ($Remover) {
    $existe = Get-ScheduledTask -TaskName $NomeTarefa -TaskPath "\$PastaTarefas\" -ErrorAction SilentlyContinue
    if ($existe) {
        Unregister-ScheduledTask -TaskName $NomeTarefa -TaskPath "\$PastaTarefas\" -Confirm:$false
        Write-Host "Removida: $NomeTarefa"
    } else {
        Write-Host "Nenhuma tarefa '$NomeTarefa' encontrada - nada a remover."
    }
    exit 0
}

$PollMinutos = Obter-PollMinutos

# -Once -At (agora) + repeticao a cada N minutos "para sempre" (RepetitionDuration
# = TimeSpan::MaxValue) e o padrao do Task Scheduler para "repita indefinidamente
# a partir de agora, a cada N minutos".
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes $PollMinutos) `
    -RepetitionDuration ([TimeSpan]::MaxValue)
$action = New-ScheduledTaskAction -Execute $Python -Argument "-m voofacil.worker --auto" -WorkingDirectory $RaizProjeto
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 5)

if ($RodarDesconectado) {
    Write-Host "Configurando '$NomeTarefa' para rodar mesmo desconectado (pedira senha da conta atual)..."
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Password -RunLevel Limited
    Register-ScheduledTask -TaskName $NomeTarefa -TaskPath "\$PastaTarefas\" `
        -Trigger $trigger -Action $action -Settings $settings -Principal $principal -Force |
        Out-Null
} else {
    Register-ScheduledTask -TaskName $NomeTarefa -TaskPath "\$PastaTarefas\" `
        -Trigger $trigger -Action $action -Settings $settings -Force |
        Out-Null
}
Write-Host "OK: $NomeTarefa a cada $PollMinutos min -> python -m voofacil.worker --auto"

Write-Host "`nPronto. Veja a tarefa em: taskschd.msc -> Biblioteca do Agendador de Tarefas -> $PastaTarefas"
Write-Host "Os horarios de varredura/explore agora sao editaveis direto no painel (aba Agendamento)."
Write-Host "Para remover: .\scripts\instalar_tarefas_windows.ps1 -Remover"
