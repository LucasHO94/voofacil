<#
.SYNOPSIS
    Registra as tarefas agendadas do VooFacil no Agendador de Tarefas do Windows.

.DESCRIPTION
    Cria/atualiza tarefas que rodam o worker em modo --once (varredura) e
    --once-explore (busca ampla) nos horarios definidos, SEM precisar manter
    um processo Python ligado o tempo todo (mais robusto que o modo daemon
    do worker.py em uma maquina Windows que pode dormir/reiniciar).

    Os horarios NAO sao mais fixos neste script: sao lidos em runtime de
    voofacil/config.py (settings.scan_horarios e settings.explore_horario,
    configuraveis via .env) atraves do Python do venv do projeto. Para
    aumentar/diminuir a quantidade de horarios de varredura, edite
    SCAN_HORARIOS no .env e rode este script de novo (ele recria as tarefas
    do zero a cada execucao).

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

if (-not (Test-Path $Python)) {
    Write-Error "venv nao encontrado em $Python. Rode primeiro: python -m venv .venv && .venv\Scripts\pip install -e .[dev]"
}

function Obter-HorariosCsv {
    <#
        Chama o Python do venv do projeto para ler settings.<Atributo> em
        runtime (voofacil.config.settings + voofacil.schedule_utils.parse_horarios)
        e devolve a lista de horarios "HH:MM" ja validada/normalizada. E isso
        que torna a lista de tarefas dinamica: muda o .env, roda o script de
        novo, sem precisar editar este arquivo.
    #>
    param(
        [Parameter(Mandatory)][string]$Atributo
    )

    $codigoPython = "from voofacil.config import settings; from voofacil.schedule_utils import parse_horarios; print(','.join(f'{h:02d}:{m:02d}' for h,m in parse_horarios(settings.$Atributo)))"
    $saida = & $Python -c $codigoPython
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($saida)) {
        Write-Error "Falha ao ler settings.$Atributo via Python (venv em $Python). Confira o .env / voofacil/config.py."
    }
    return ($saida.Trim() -split ",")
}

$HorariosVarredura = Obter-HorariosCsv -Atributo "scan_horarios"
$HorariosExplore = Obter-HorariosCsv -Atributo "explore_horario"

$Tarefas = @()
foreach ($hora in $HorariosVarredura) {
    $sufixo = $hora -replace ":", ""
    $Tarefas += @{ Nome = "VooFacil-Varredura-$sufixo"; Hora = $hora; Args = "-m voofacil.worker --once" }
}
foreach ($hora in $HorariosExplore) {
    $sufixo = $hora -replace ":", ""
    $Tarefas += @{ Nome = "VooFacil-Explore-$sufixo"; Hora = $hora; Args = "-m voofacil.worker --once-explore" }
}

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
