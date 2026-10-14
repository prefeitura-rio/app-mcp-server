# Filtro eleitoral na busca do MCP: decisões e colisões aceitas

**Jira:** [CHATR-294](https://iplanrio-pcrj.atlassian.net/browse/CHATR-294)
**Ramo:** `fix/CHATR-294-filtro-eleitoral-google-search`
**Data:** 14 de outubro de 2026
**Status:** implementado, aguardando revisão de código e aprovação da lista
**Incidente de origem:** conversa de 09/10/2026 em que o chatbot informou o número eleitoral de um candidato à Presidência e disse não encontrar o do outro (relatório anexado ao card).

---

## Resumo

A tool `google_search` passa a ter um filtro determinístico antes de qualquer buscador. Uma query com termo eleitoral, ou com o nome de um candidato do 2º turno, não chega ao Typesense nem ao Gemini e recebe um texto fixo, igual para qualquer candidato.

Este documento registra cada decisão tomada, as alternativas descartadas e as colisões aceitas conscientemente, para dar transparência ao processo antes da aprovação.

Artefatos:

| O quê | Onde |
| --- | --- |
| Listas (versionadas, revisadas em PR) | [`src/tools/google_search/query_filter.yaml`](../../src/tools/google_search/query_filter.yaml) |
| Filtro | [`src/tools/google_search/query_filter.py`](../../src/tools/google_search/query_filter.py) |
| Ponto de aplicação | `get_google_search` em [`src/tools/search.py`](../../src/tools/search.py) |
| Dataset rotulado (260 queries) e runner | [`src/tests/e2e/filtro_query/`](../../src/tests/e2e/filtro_query/) |

---

## Premissas

- **A knowledge base é consultada antes do MCP.** Só chega ao filtro o que ela não respondeu. Por isso um bloqueio indevido aqui custa menos do que um tema eleitoral que escapa.
- **O período é eleitoral.** O 2º turno é em 25/10/2026. Várias decisões abaixo partem disso: nesse período, "segundo turno" e "votação" quase sempre falam de eleição.
- **A query que chega é a mensagem do cidadão, sem reescrita.** O filtro não tem o contexto da conversa.

---

## Decisões

### D1. Três camadas de proteção

**Decisão:** (1) o prompt do agente orienta a não responder sobre tema eleitoral; (2) um subagent offtopic responde que o tema não faz parte do catálogo de serviços; (3) o MCP filtra a busca. Este card cobre a camada 3.
**Motivo:** prompt sozinho não basta. No incidente, a diferença entre os candidatos veio da variação do modelo e do ranking do Google, não de uma instrução. Só uma barreira determinística garante o mesmo tratamento para todos.
**Decidido por:** time.

### D2. Whitelist e blacklist coexistem; a blacklist sempre pode vetar

**Decisão:**

| Configuração | Comportamento |
| --- | --- |
| Nenhuma lista | A busca opera normalmente |
| Só blacklist | Barra a query que contém um termo da lista |
| Só whitelist | Só passa a query que casa com uma expressão da lista |
| As duas | A whitelist decide o que entra e a blacklist ainda pode vetar |

**Alternativa descartada:** com as duas listas, seguir só a whitelist (proposta inicial).
**Motivo:** um tema permitido pode vir junto de um termo proibido ("o candidato prometeu zerar o IPTU, qual o número dele?"). O veto da blacklist não custa nada e não depende de a whitelist ser perfeita.
**Decidido por:** Mauro.

### D3. Whitelist em regex; blacklist por palavra inteira, sem regex

**Decisão:** a whitelist é genérica e usa expressões regulares, para autorizar variações dos temas permitidos. A blacklist corta termos específicos. Ela compara palavra inteira, depois de normalizar o texto: minúsculas, sem acento, sem pontuação e sem caracteres invisíveis.
**Alternativa descartada:** "contém" por substring.
**Motivo:** substring barraria serviço essencial. "celular" contém "lula", "tomar" contém "omar" e "cadunico" contém "cadu".
**Decidido por:** Mauro.

### D4. Começar só com a blacklist

**Decisão:** a whitelist entra vazia. A lista de temas permitidos ainda não está disponível, e a previsão é de alguns dias.
**Decidido por:** time.

### D5. Listas em arquivo versionado, não em variável de ambiente

**Decisão:** as listas ficam em `query_filter.yaml`, no repositório.
**Alternativa descartada:** variável de ambiente no Infisical. Ela permitiria mudar sem deploy, mas qualquer mudança passaria sem revisão.
**Motivo:** a lista é politicamente sensível. Toda mudança precisa passar por PR revisado e ficar no histórico.
**Decidido por:** Mauro.

### D6. Candidatos: todos os do 2º turno, com a mesma regra

**Decisão:** a lista cobre os 14 candidatos do 2º turno de 25/10/2026: Presidente e Governador em AC, AM, DF, ES, RN e TO. A fonte é o arquivo de dados abertos do TSE, `consulta_cand_2026`, gerado em 14/10/2026.

- Quando o primeiro nome é comum, ele entra composto ("alan rick", não "alan").
- O número de urna (13, 22…) **não** entra, porque barraria qualquer query com número.

**Motivo:** incluir um candidato e esquecer o adversário recriaria a assimetria que originou o incidente.

| Cargo | Candidato (nome de urna) | Partido / nº | Termos |
| --- | --- | --- | --- |
| Presidente | LULA | PT 13 | `lula`, `luiz inácio`, `luis inácio` |
| Presidente | FLAVIO BOLSONARO | PL 22 | `bolsonaro` |
| Gov. AC | MAILZA ASSIS | PP 11 | `mailza` |
| Gov. AC | ALAN RICK | Republicanos 10 | `alan rick` |
| Gov. AM | OMAR AZIZ | PSD 55 | `omar aziz`, `abdelaziz` |
| Gov. AM | PROFESSORA MARIA DO CARMO | PL 22 | `professora maria do carmo é candidata`, `número da professora maria do carmo`, `seffair` |
| Gov. DF | CELINA LEÃO | PP 11 | `celina leão` |
| Gov. DF | LEANDRO GRASS | PT 13 | `leandro grass`, `grass` |
| Gov. ES | LORENZO PAZOLINI | Republicanos 10 | `pazolini` |
| Gov. ES | RICARDO FERRAÇO | MDB 15 | `ferraço` |
| Gov. RN | ALLYSON | União 44 | `allyson bezerra`, `número do allyson` |
| Gov. RN | CADU DE LULA | PT 13 | `cadu xavier`, `número do cadu` (o nome de urna já cai em `lula`) |
| Gov. TO | PROFESSORA DORINHA | União 44 | `dorinha` |
| Gov. TO | VICENTINHO JÚNIOR | PSDB 45 | `vicentinho` |

`bolsonaro` também barra menções ao Jair Bolsonaro, que não é candidato. Isso foi aceito: o bot municipal não deve tratar de nenhum dos dois.
**Decidido por:** Mauro. A lista precisa de validação de negócio antes do deploy.

### D7. Termos eleitorais e termos deixados de fora

**Decisão:** a blacklist cobre eleição e eleitor, voto, documentos e local de votação, Justiça Eleitoral, candidatura, campanha, turnos, nomes de partido sem colisão e institutos de pesquisa. A lista completa está no YAML.

Ficaram de fora porque barrariam serviço legítimo:

| Termo | Colisão |
| --- | --- |
| `presidente` | Avenida Presidente Vargas |
| `governador` | Ilha do Governador |
| `urna` sozinha | Cemitério (urna cinerária) |
| `campanha` | Campanha de vacinação |
| `candidato` / `candidata` sozinhos | Concurso público, vaga em creche |
| `pt`, `pl`, `pp`, `novo`, `rede`, `podemos` | Siglas e palavras comuns ("PL" é projeto de lei) |
| `turno`, `título`, `cola`, `biometria`, `direita`, `esquerda` | Palavras comuns |

**Decidido por:** Mauro.

### D8. Revisão da lista depois do dataset: cargos só em expressão eleitoral

**Decisão:** esta decisão **reverte em parte** uma anterior. A primeira versão bloqueava `deputado`, `senador` e `vereador` sozinhos.

- Esses cargos saíram como palavra solta e só entram em expressão eleitoral: `deputado federal`, `senador eleito`, `deputados eleitos`, `mais votado`, `candidata a senadora`.
- `vereador` saiu de vez, porque não há eleição de vereador em 2026.
- `apuração` sozinho saiu. "Apuração dos votos" continua barrado por `votos`.
- `professora maria do carmo` virou duas expressões.

**Motivo:** o dataset mostrou que "Rua Senador Dantas", "Rua Senador Vergueiro", "Rua Deputado Soares Filho", "apuração da denúncia na ouvidoria" e "a professora Maria do Carmo da escola do meu filho" seriam barrados.
**Decidido por:** Mauro.

### D9. Ampliação: flexões, derivados, inglês e grafias

**Decisão:** entraram na lista:
- flexões de votar (`vote`, `votou`, `votando`, `votaria`…);
- derivados (`bolsonarista`, `lulista`, `petista`, `eleitoreiro`);
- termos em inglês (`election`, `voting`);
- grafias quebradas ou trocadas (`l u l a`, `lu la`, `lula13`, `bolsonaru`, `eleisão`).

**Motivo:** a comparação por palavra inteira não infere variações. Cada variação precisa estar listada.
**Limite conhecido:** a seção de grafias cobre as variações que estão no dataset, não todas as possíveis. Esse problema se resolve de forma ampla com regex, na whitelist.
**Decidido por:** Mauro.

### D10. Colisões aceitas conscientemente

As queries abaixo não são eleitorais e **continuam barradas**. Resolver qualquer uma delas exigiria tirar um termo central da lista (`lula`, `eleição`, `votação`, `segundo turno`), e as perguntas do incidente voltariam a passar.

| Colisão | Exemplo | Motivo para manter o bloqueio |
| --- | --- | --- |
| Serviço social citando o presidente | "como receber o Bolsa Família do Lula" | `lula` é o termo central do incidente. A knowledge base responde antes |
| Fruto do mar | "onde comprar lula no mercado municipal" | Idem |
| Turno escolar | "escola municipal no segundo turno da tarde" | No período eleitoral, "segundo turno" é quase sempre eleição. Pode ser revisto depois de 25/10 |
| Eleição que não é a geral | ônibus grátis no dia da eleição, diretor de escola, conselho tutelar, síndico | O que precisar ser informado sobre esses temas virá do FAQ |
| Participação cidadã | "votação do orçamento participativo" | No período eleitoral, as perguntas com "votação" são sobre eleição |

**Decidido por:** Mauro.

### D11. Resposta ao bloqueio

**Decisão:**
- **Texto:** é fixo e igual para qualquer tema e qualquer candidato. Variar o texto por candidato recriaria a assimetria. Texto aprovado:

  > Busca não realizada: o tema está fora do catálogo de serviços da Prefeitura do Rio. Responda ao cidadão que não é possível ajudar com esse assunto por aqui, sem citar candidatos, partidos, números ou resultados, e ofereça ajuda com os serviços municipais. Não refaça a busca com outras palavras.

- **Destinatário:** quem lê o texto é o agente, não o cidadão. Por isso ele é uma instrução. "Não refaça a busca com outras palavras" impede que o agente reformule a query para contornar o filtro.
- **Formato:** a resposta sai como sucesso, com o mesmo formato da busca normal, e não como `isError`. É recusa de tema, não falha técnica, e o Salesforce contaria `isError` como falha.
- **Sem parte dinâmica:** a query, o termo que casou e o mecanismo não vão para o agente. Se o agente repetisse o termo ao cidadão, a resposta variaria por candidato. A query o agente já tem, e o diagnóstico fica no span e no BigQuery (D12).

**Alternativa descartada:** "Busca bloqueada pelos filtros do MCP". É uma frase técnica que o agente pode repassar literalmente ao cidadão, e não diz o que ele deve fazer.
**Decidido por:** Mauro.

### D12. Observabilidade

**Decisão:**
- **Span:** toda chamada da tool registra `search.filter.blocked`. As barradas também registram `search.filter.reason` e `search.filter.matched`.
- **BigQuery:** cada bloqueio vira uma linha em `brutos_eai_logs.mcp`, com `source = "query_filter"`, a query e os campos `blocked`, `filter_reason` e `filter_matched`.
- **Log:** uma linha por bloqueio.

**Alternativa adiada:** métrica OTel para alerta (pico de bloqueios ou bloqueio caindo a zero). Para o volume atual, um painel ou alerta no SigNoz sobre o atributo do span resolve sem código novo.
**Decidido por:** Mauro.

### D13. O filtro cobre só a `google_search`

**Decisão:** o filtro é aplicado apenas na `google_search`. As outras tools de busca livre, `web_search_surkai` e `dharma_search_tool`, estão desligadas em produção.
**Evidência:** `EXCLUDED_TOOLS` de produção, conferido em 14/10/2026: `user_feedback,web_search_surkai,dharma_search_tool,greeting_format`.
**Condição:** se alguma dessas tools voltar a ser habilitada, ela vira um caminho de busca sem filtro e precisa receber o mesmo filtro antes de entrar.
**Decidido por:** Mauro.

### D14. Configuração malformada impede o pod de subir

**Decisão:** YAML inválido ou regex inválida na whitelist impedem o pod de subir. O teste unitário carrega o arquivo real, então esse erro aparece no CI, antes do deploy.
**Motivo:**
- Ignorar uma expressão inválida em silêncio deixaria a whitelist diferente da que foi revisada.
- Ignorar o arquivo inteiro desligaria o filtro sem ninguém perceber.

---

## Validação

O dataset rotulado ([`dataset.csv`](../../src/tests/e2e/filtro_query/dataset.csv)) tem 260 queries. O rótulo é o comportamento **desejado**, não o atual, para que lacunas e colisões apareçam como divergência.

| Categoria | Linhas | Esperado |
| --- | --- | --- |
| Incidente de 09/10 (turnos e reprodução) | 9 | 8 bloquear, 1 permitir ("Qual o número dele?") |
| Candidatos (Presidente e Governador) | 47 | bloquear |
| Termos eleitorais | 71 | bloquear |
| Mutações (acento, caixa, pontuação, grafia, flexão, inglês) | 42 | bloquear |
| Colisões aceitas (D10) | 10 | bloquear |
| Falsos positivos evitados | 36 | permitir |
| Serviços municipais comuns | 35 | permitir |
| Política sem termo da lista | 8 | permitir |
| Decisão pendente (RJ) | 2 | permitir |

**Resultado em 14/10/2026: 260 de 260 iguais ao rótulo.** O mesmo dataset roda contra o MCP local. Nas linhas do incidente, os bloqueios voltaram em 6 a 16 ms, sem chamar o Gemini.

## Limitações conhecidas

- **Pronome e número sem contexto passam.** Exemplos: "Qual o número dele?", "13 ou 22?", "em quem você vota?". Nenhum termo identifica o tema, e quem trata esses casos são as camadas 1 e 2, porque só o agente tem a conversa.
- **Uma grafia nova escapa** até ser listada ou até a whitelist em regex entrar.

## Pendências

| Pendência | Responsável | Bloqueia o quê |
| --- | --- | --- |
| Validação de negócio da lista de candidatos (D6) | A definir | Deploy em produção |
| RJ: Douglas Ruas foi declarado eleito em 10/10, mas havia recurso do Eduardo Paes. Se houver 2º turno no RJ, os dois entram na lista, e bloquear "Eduardo Paes" no bot da Prefeitura exige decisão própria | A definir | Nada, por enquanto |
| Definir o que acontece com a blacklist depois da eleição de 25/10: candidatos e termos ficam, saem ou são revistos | Área demandante, depois da eleição | Nada, por enquanto |
| Conteúdo da whitelist. Ainda não foi entregue. Quando chegar, entra por PR no `query_filter.yaml` e exige uma nova versão da aplicação, porque as listas são lidas só na subida do pod (D5, D14) | Área demandante | Nada, por enquanto |
