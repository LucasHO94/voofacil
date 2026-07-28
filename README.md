# VooFácil — monitor de passagens aéreas

Bot pessoal que monitora preços de passagens aéreas (rota específica ou
origem → região/país inteiro), guarda o histórico e alerta por Telegram e
e-mail quando o preço cai abaixo de um limite ou do menor valor já visto.

Projeto independente — não compartilha código, banco ou repositório com
nenhum outro projeto do workspace.

## 1. Instalação

```bash
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
copy .env.example .env
```

Edite o `.env` com suas chaves (nenhuma é obrigatória para começar — o app
degrada graciosamente sem elas, só não varre/alerta):

| Variável | Onde conseguir | Obrigatória? |
|---|---|---|
| `TP_TOKEN` | Cadastro grátis em [travelpayouts.com](https://www.travelpayouts.com) (programa de afiliados Aviasales) → token no painel | Sim, para varrer preços |
| `SERPAPI_KEY` | Cadastro em [serpapi.com](https://serpapi.com) (free tier 250 buscas/mês) | Não — sem ela, os alertas saem sem confirmação ao vivo |
| `TELEGRAM_TOKEN` / `TELEGRAM_CHAT_ID` | Fale com `@BotFather` no Telegram (`/newbot`), depois mande `/start` ao bot e leia seu `chat_id` em `https://api.telegram.org/bot<TOKEN>/getUpdates` | Não — sem ela, sem alerta no Telegram |
| `SMTP_USER` / `SMTP_PASSWORD` / `ALERT_EMAIL_TO` | Uma senha de app do Gmail (não a senha normal da conta) | Não — sem ela, sem alerta por e-mail |

**Nunca** commite o `.env` nem o banco (`data/*.db`) — já protegidos pelo
`.gitignore`. Antes de qualquer `git push`, rode `git ls-files` e confira.

## 2. Uso do dia a dia

### Painel (criar/ver monitoramentos)

```bash
.venv\Scripts\streamlit run voofacil\app\Painel.py
```

Abre em `http://localhost:8501` (ou a porta configurada). Lá dá para criar
watches de rota específica ou de região, ver o histórico de preços em
gráfico e a lista de alertas enviados. O botão "Varrer agora" roda uma
varredura pontual — a varredura periódica de verdade é o worker (seção 3).

### Linha de comando (alternativa ao painel)

```bash
.venv\Scripts\python scripts\seed_watch.py rota GRU LIS 2026-09-01 2026-09-30 --limite 3500
.venv\Scripts\python scripts\seed_watch.py regiao GRU europa 2026-09-01 2026-11-30 --limite 4000
.venv\Scripts\python scripts\seed_watch.py listar
```

Regiões disponíveis: `brasil_capitais`, `nordeste`, `america_sul`, `europa`,
`america_norte`, `asia` (editável em `scripts/seed_watch.py`, dict `REGIOES`).
Só a região `europa` tem `kgmid` cadastrado para a busca ampla via SerpApi
Explore (`ARRIVAL_AREA_ID`); as demais funcionam com a lista curada, só sem
o Explore diário.

## 3. Rodando a varredura periódica

O worker é um **processo separado** do painel — nunca roda dentro do
Streamlit. Duas formas de operar, escolha uma:

### Opção A — Windows Task Scheduler (recomendada para uso contínuo)

```powershell
.\scripts\instalar_tarefas_windows.ps1
```

Cria **uma única** tarefa agendada (`VooFacil-Poll`) rodando
`python -m voofacil.worker --auto` a cada `agendamento_poll_minutos` minutos
(15 por padrão) indefinidamente. A cada execução o worker consulta o banco e
decide sozinho se é hora de varrer e/ou explorar de verdade (ver "Mudando os
horários" abaixo) — a tarefa em si nunca muda, só o banco. Por padrão só roda
com você logado (sem senha, sem admin). Para rodar mesmo desconectado, use
`-RodarDesconectado` (pede senha da sua conta e precisa de PowerShell como
administrador). Para remover: `.\scripts\instalar_tarefas_windows.ps1 -Remover`.

Veja/gerencie em `taskschd.msc` → pasta `VooFacil`.

### Opção B — Worker como daemon (rodar manualmente ou via serviço)

```bash
.venv\Scripts\python -m voofacil.worker            # fica rodando: poll a cada agendamento_poll_minutos, chama job_auto()
.venv\Scripts\python -m voofacil.worker --auto      # roda job_auto() uma vez (mesma decisão do poll) e sai
.venv\Scripts\python -m voofacil.worker --once      # varredura única, sai (ignora o banco — sempre roda)
.venv\Scripts\python -m voofacil.worker --once-explore   # busca ampla única, sai (idem)
```

Use esta opção se preferir manter um terminal/serviço próprio ligado (ex.:
numa VPS Linux com `systemd`/`screen`, em vez do Task Scheduler do Windows).
`--once`/`--once-explore` continuam existindo tal qual antes — não dependem
do banco nem do horário atual, servem para teste manual e para o botão
"Varrer agora" do painel.

### Mudando os horários

Os horários de varredura e de busca ampla (explore) agora são **editáveis
direto no painel Streamlit, na aba Agendamento** — adicionar, remover,
ativar/desativar e alterar hora/minuto não exige mexer em código nem
reiniciar nada, tanto localmente quanto na nuvem (o painel e o worker
compartilham o mesmo banco).

O `.env` (`SCAN_HORARIOS`/`EXPLORE_HORARIO`) só serve de **semente inicial**:
na primeira vez que o worker roda (`garantir_horarios_padrao`), se a tabela
de horários estiver totalmente vazia para aquele tipo (VARREDURA ou
EXPLORE), ela é populada a partir do `.env`. Depois disso o banco manda — se
você apagar tudo de propósito pelo painel, não volta a semear sozinho.

```bash
# .env — só usado para semear o banco na primeira execução
SCAN_HORARIOS=06:00,12:30,21:00   # CSV "HH:MM,HH:MM,..." — seed inicial da VARREDURA
SCAN_DIAS_SEMANA=*                 # "*"=todos os dias, ou "mon,tue,wed,thu,fri" (NÃO editável pelo painel)
EXPLORE_HORARIO=05:00              # seed inicial do EXPLORE
```

`SCAN_DIAS_SEMANA` é a única parte do agendamento antigo que continua vindo
só do `.env` — dia da semana não é editável pelo painel de propósito.

O padrão de horários semeado (madrugada/meio-dia/noite) só reflete um
espalhamento ao longo do dia — **não há evidência forte de que uma hora
específica tenha preço menor**. A pesquisa que embasou este projeto indica
que dia da semana e antecedência da compra pesam mais do que a hora exata da
busca; o espalhamento serve pra fisgar quedas de preço que aparecem/somem ao
longo do dia e para não concentrar todas as chamadas de API no mesmo
horário.

Tanto a nuvem (GitHub Actions, ver `.github/workflows/worker.yml`) quanto o
modo daemon local usam o **mesmo mecanismo de poll**: rodam
`voofacil.worker.job_auto()` com frequência fixa e curta — a cada 15 minutos
na nuvem (cron `*/15 * * * *`, fixo de propósito, nunca precisa mudar), a
cada `agendamento_poll_minutos` no daemon local — e é o `job_auto()` que
decide, olhando o banco (`deve_rodar_agora`), se dispara a varredura e/ou o
explore agora. Quando não há nada agendado para o momento, ele sai rápido
sem gastar nenhuma chamada de API.

## 4. Como funciona a detecção de "bom preço"

Camadas avaliadas em ordem (a primeira que disparar vence):

1. **ERROR_FARE** — preço ≤ 50% da média histórica (tarifa-erro, alerta imediato)
2. **LIMITE** — preço ≤ limite fixo que você definiu no watch
3. **NOVO_MINIMO** — preço abaixo do menor já registrado (com margem de 2% contra ruído)
4. **PERCENTIL** — preço entre os 25% mais baratos do histórico da rota+mês (só ativa após ~20 amostras, indicado sobretudo para watches de região sem limite fixo)

Cada rota tem cooldown de 12h: não alerta de novo a menos que o preço caia
ainda mais. Todo alerta tenta confirmação ao vivo via SerpApi antes de sair
(se configurada e dentro do orçamento) — sem confirmação, o alerta avisa
que o preço é de cache e pode ter mudado.

### Orçamento da SerpApi (mensal + diário)

A SerpApi é chamada em 3 pontos — não só quando acha um preço bom:

1. **Confirmação de candidato** (`confirmar_candidatos`) — só quando o
   detector já sinalizou um preço bom, antes de alertar.
2. **Varredura regular multi-fonte** (`sweep()`, watches ROTA) — em
   **toda** varredura, para comparar Travelpayouts + Google Flights lado a
   lado ("estilo Skyscanner"), independente do preço.
3. **Busca ampla diária** (`executar_explore`, watches REGIAO) — 1x/dia
   por região cadastrada, também independente do preço.

Por isso existem DOIS limites, aplicados juntos (basta um estourar para
bloquear): `serpapi_orcamento_mes` (free tier, padrão 250/mês) e
`serpapi_orcamento_dia` (padrão 8/dia — evita que uma varredura com muitos
watches ROTA gaste o mês inteiro em poucos dias). Sem orçamento, a SerpApi
simplesmente some da rodada — o alerta continua saindo, só sem confirmação
ao vivo.

Esses parâmetros (cooldown, margens, orçamento mensal/diário) ficam em
`voofacil/config.py` e podem ser sobrescritos no `.env`
(`SERPAPI_ORCAMENTO_MES`, `SERPAPI_ORCAMENTO_DIA`).

## 5. Diagnóstico

- **"Falha calendar ... TP_TOKEN ausente"**: normal sem token configurado; a
  varredura roda mas não traz cotações.
- **Endpoints da Travelpayouts podem ter migrado de versão** — antes de
  confiar no monitoramento, rode:
  ```bash
  .venv\Scripts\python scripts\smoke_tp.py GRU LIS
  ```
  Se nenhum endpoint responder, o cliente em `voofacil/providers/travelpayouts.py`
  precisa ser adaptado à API nova (ver comentários no topo do arquivo).
- **Testes**: `.venv\Scripts\python -m pytest -q` (tudo offline, sem chamar
  APIs reais).
