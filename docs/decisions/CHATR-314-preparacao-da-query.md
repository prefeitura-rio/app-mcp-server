# Preparação da query da busca do MCP: mensagem sem assunto, dado pessoal e contexto

**Jira:** [CHATR-314](https://iplanrio-pcrj.atlassian.net/browse/CHATR-314)
**Ramo:** `feat/CHATR-314-preparacao-query-google-search`
**Data:** 26 de outubro de 2026
**Status:** implementado no MCP, aguardando revisão de código. A parte do Salesforce (campo `contexto` na action e instrução do agente) é outro PR.

---

## Resumo

A tool `google_search` passa a preparar a query antes de qualquer buscador:

1. **F1, mensagem sem assunto:** `Sim`, `Ok`, `2027`, `[ATTACHMENTS]` e `Por favor` não são pesquisados. O agente recebe um texto que pede para refazer a busca com o assunto da conversa.
2. **F4-C, dado pessoal:** CPF, endereço, nome, data de nascimento, CEP, placa, e-mail e telefone saem da query antes de ela ir para o Typesense e o Gemini. O BigQuery passa a guardar só a versão redigida.
3. **F4-B, contexto:** a tool aceita `contexto` (opcional), o assunto da conversa em uma frase. A busca usa a query junto dele.

Ordem em `get_google_search`: filtro → redação de `query` e `contexto` → composição do tema → Typesense e Gemini → BigQuery.

Este documento registra as decisões, as alternativas descartadas e os limites conhecidos. Complementa o [CHATR-294](CHATR-294-filtro-eleitoral.md), que criou o filtro de query.

Artefatos:

| O quê | Onde |
| --- | --- |
| Listas do filtro sem assunto (versionadas, revisadas em PR) | seção `sem_assunto` de [`query_filter.yaml`](../../src/tools/google_search/query_filter.yaml) |
| Filtro (`SemAssuntoFilter`, `SearchFilter`) | [`src/tools/google_search/query_filter.py`](../../src/tools/google_search/query_filter.py) |
| Redação (`redigir_query`) | [`src/utils/pii.py`](../../src/utils/pii.py) |
| Ponto de aplicação e composição do tema | `get_google_search` e `compor_tema` em [`src/tools/search.py`](../../src/tools/search.py) |
| Assinatura e docstring da tool | `google_search` em [`src/app.py`](../../src/app.py) |

---

## Premissas

- **Medição:** 6.124 buscas web de 19 a 26/10/2026 para o F1; 15.550 buscas de outubro para o F4-C.
- **Quem lê a resposta de bloqueio é o agente**, não o cidadão. O texto é instrução.
- **O Salesforce ainda não envia `contexto`.** Até ele enviar, a tool se comporta como hoje, mais o filtro sem assunto e a redação.

---

## F1. Mensagem sem assunto

### D1. Quatro regras, nesta ordem

**Decisão:** a primeira regra que casa decide.

| Regra | Id no registro | O que barra | Buscas na semana | Sem resposta útil hoje |
| --- | --- | --- | --- | --- |
| A. Placeholder ou vazio | `placeholder` | `[ATTACHMENTS]` ou outro token entre colchetes; texto vazio | 52 (0,8%) | 92,3% |
| B. Sem nenhuma letra | `sem_letra` | `1`, `2027`, `?`, CPF, data, emoji. Exceção: `1746` | 62 (1,0%) | 79,0% |
| C. Só uma expressão da lista fechada | `expressao` | `Sim` (184×), `Ok`, `Entendi`, `Obrigada`, `Me ajuda`, `Boa tarde` | 220 (3,6%) | 30,0%* |
| D. Só palavras do vocabulário sem assunto, até 8 | `vocabulario` | `Sim quero saber`, `Saber mais`, `Por favor`, `Não é isso` | 52 (0,8%) | 92,3% |
| **Total** | | | **386 (6,3%)** | |

\* As "respostas" a `Sim` são pesquisas pela palavra solta (ex.: "what does sim mean in brazilian"). Contam como respondidas, mas não servem ao cidadão.

- C e D comparam o texto normalizado como o filtro eleitoral (minúsculas, sem acento, pontuação vira espaço).
- Uma palavra de fora das listas basta para a mensagem passar. `Creche`, `IPTU`, `Documentos`, `Prazos`, `Para 2027` e `Quero remarcar` passam: dependem da conversa, mas têm assunto.
- D só vale até 8 palavras. Com mais, a chance de haver assunto cresce, e barrar por engano custa mais.

**Alternativas descartadas:**
- Só um link (4 buscas): o cidadão pode estar perguntando sobre a página. Ganho pequeno e ambíguo.
- Risada ou interjeição sozinha: nenhuma ocorrência na semana.
- Fragmentos com assunto (`Documentos`, `Para 2027`; 412 buscas de até duas palavras): não se barram. A correção é o agente incluir o assunto, e o campo `contexto` (F4-B) dá o caminho.

**Decidido por:** Mauro (regras e medição, 26/10/2026).

### D2. Sem assunto antes do eleitoral; sem a seção no YAML, nada muda

**Decisão:** `SearchFilter` monta `[SemAssuntoFilter, filtro eleitoral]`. A primeira decisão que barra vence. Sem a seção `sem_assunto` no YAML (ou com ela vazia), o filtro é só o eleitoral, como no CHATR-294.
**Motivo:** a mensagem sem assunto não tem como conter tema eleitoral, então a ordem não muda nenhuma decisão. Ela só define o motivo registrado.
**Decidido por:** especificação do card.

### D3. Placeholder sai antes das outras regras

**Decisão:** a regra A tira os tokens entre colchetes e avalia o que sobra. As regras B, C e D avaliam esse resto. `[ATTACHMENTS] Sim` é barrado pela C; `[ATTACHMENTS] segunda via do IPTU` passa.
**Alternativa descartada:** avaliar B, C e D no texto com o placeholder. "attachments" tem letra e não está nas listas, então `[ATTACHMENTS] Sim` passaria.
**Motivo:** o placeholder é o canal no lugar do anexo, não o cidadão. Tirá-lo não remove assunto nenhum.
**Decidido por:** Claude, sem revisão humana. Reverter é trocar a ordem em `SemAssuntoFilter.check`.

### D4. Listas no YAML versionado, revisadas em PR

**Decisão:** seção `sem_assunto` em `query_filter.yaml`: `placeholder` (expressão sobre o texto cru), `numeros_permitidos`, `expressoes`, `vocabulario` e `max_palavras`. YAML malformado ou expressão inválida impedem o pod de subir, como no CHATR-294 (D14 de lá).
**Motivo:** o mesmo do CHATR-294 (D5 de lá): mudança de lista precisa de revisão e histórico.

O conteúdo de `expressoes` e `vocabulario` foi montado a partir dos exemplos da especificação. Nenhuma palavra das listas pode ter assunto: incluir "matrícula" no vocabulário barraria "matrícula sim". O dataset rotulado do CHATR-294 (260 queries) continua com 260 de 260 iguais ao rótulo com a seção ligada.
**Decidido por:** Claude, sem revisão humana (conteúdo das listas). Pendente de validação.

### D5. Um texto de resposta por motivo

**Decisão:**
- **Eleitoral:** o texto do CHATR-294 (D11 de lá), sem mudança. Proíbe refazer a busca.
- **Sem assunto:**

  > Busca não realizada: a mensagem sozinha não tem um assunto para pesquisar. Refaça a busca incluindo o assunto da conversa ou pergunte ao cidadão qual é a dúvida.

  Ao contrário do eleitoral, permite refazer a busca: o assunto existe, só ficou na conversa.
- **Motivo desconhecido** (um filtro futuro sem texto próprio): recebe o texto eleitoral, o único que proíbe refazer a busca. Na dúvida, fica o lado que não abre brecha.
- As duas respostas saem como sucesso, sem `isError`, pelo mesmo motivo do CHATR-294.

**Decidido por:** especificação do card (textos); Claude (motivo desconhecido).

### D6. Registro

**Decisão:** a mesma trilha do filtro eleitoral (CHATR-294, D12 de lá).
- **BigQuery:** `source = "query_filter"`, `filter_reason = "sem_assunto"` e `filter_matched` com o id da regra.
- **Span:** `search.filter.reason` e `search.filter.matched` com o motivo e a regra.
- Nunca o texto da query no span.

---

## F4-C. Redação de dado pessoal

### D7. Tirar quem é a pessoa, manter do que ela está falando

**Decisão:** `redigir_query` em `src/utils/pii.py` redige:

| Tipo (registro) | Marcador | Regra |
| --- | --- | --- |
| `email` | `[e-mail]` | padrão existente do `pii.py` |
| `cpf` | `[cpf]` | padrão existente, **só com dígito verificador válido** |
| `cep` | `[cep]` | com hífen (`22290-140`, `22.290-140`) ou ao lado da palavra "CEP" |
| `telefone` | `[telefone]` | padrões existentes (formato brasileiro e com gatilho) |
| `placa` | `[placa]` | antiga com hífen, antiga sem hífen em maiúsculas, Mercosul |
| `endereco` | `[endereço]` | tipo de logradouro + nome + número (+ complemento). O bairro fica |
| `data_nascimento` | `[data]` | data só a até 25 caracteres de "nasceu", "nascido", "nascimento" |
| `nome` | `[nome]` | depois de gatilho (ver D8) |

Não redige:
- protocolo do 1746, inscrição imobiliária e CPF com dígito verificador inválido, que são o assunto da busca;
- data de prazo ("inscrições até 30/10");
- bairro, porque localiza a resposta;
- **dado de saúde** (TEA, gravidez, deficiência). Costuma ser o próprio assunto ("transferência de escola para aluno com TEA"), e sem ele a busca perde o sentido.

- **Marcadores:** curtos, em português, sem dígito e sem gatilho. Assim o modelo de busca entende o que havia ali, e a função é idempotente.
- **Retorno:** a função devolve o texto e os tipos redigidos, para o span e o BigQuery.

**Decidido por:** especificação do card (princípio e tipos); Claude (detalhes de cada padrão, D8).

### D8. Detalhes dos padrões, decididos na implementação

Todos decididos por Claude, sem revisão humana:

- **Nome declarado** ("meu nome é", "me chamo"): até 4 palavras em qualquer caixa. Para em pontuação, número ou palavra de frase ("e", "quero", "moro"...).
- **Nome de familiar e apresentação** ("meu filho", "minha filha", "sou a"...): só palavras capitalizadas (`Ana`, não `ANA` nem `ana`).
  - Motivo: "meu filho tem TEA" e "sou a responsável" não têm nome, e sem a exigência "tem TEA" seria apagado.
  - Palavras comuns capitalizadas ("Mãe", "Responsável", "Aluno") não contam como nome.
- **Endereço:** os tipos de logradouro são rua, R., avenida, av, estrada, travessa, praça, largo, alameda, rodovia, ladeira, beco e servidão.
  - Ficaram de fora "vila", que também é bairro ("Vila Isabel"), "via", de "segunda via do IPTU 2026", e "caminho".
  - Nome com palavra de frase ("tem", "minha", "para"...) não é endereço: "a rua da minha casa tem 3 buracos" passa intacta.
- **Placa antiga sem hífen só em maiúsculas:** em minúsculas, "rio2016" viraria placa.
- **CEP sem hífen** só com a palavra "CEP" ao lado: oito dígitos soltos podem ser inscrição.
- **CNPJ não é redigido.** Não está na lista do card. Identifica empresa e pode ser o assunto ("alvará do CNPJ"). Ver pendências.

### D9. Estender o `pii.py`, sem biblioteca nova

**Decisão:** padrões novos no `pii.py`, só stdlib.
**Alternativa descartada:** [Presidio](https://presidio.dataprivacystack.org/supported_entities/). Não tem reconhecedor brasileiro (CPF, CNPJ, RG, telefone BR). Para nome e lugar, exige um modelo grande do spaCy em português dentro do container.
**Motivo:** para 2% das buscas, com padrões conhecidos, estender a fonte única de padrões do projeto dá o mesmo resultado com menos peso. Reavaliar se a detecção de nome sem gatilho virar requisito.
**Decidido por:** Mauro (26/10/2026).

### D10. O filtro olha o texto cru; o resto, só o redigido

**Decisão:** o filtro roda antes da redação. Buscadores, BigQuery (inclusive no caminho de bloqueio) e span só recebem a versão redigida.
**Motivo:** a regra B precisa ver o CPF solto. Redigido, `529.982.247-25` viraria `[cpf]`, que tem letra e passaria. Gravar a versão redigida também no bloqueio é o lado conservador: a linha do BigQuery não precisa do texto cru para diagnosticar o filtro, porque o motivo e a regra estão nela.
**Decidido por:** Claude, sem revisão humana (o card deixou o bloqueio em aberto).

---

## F4-B. Campo opcional de contexto

### D11. Composição do tema

**Decisão:** sem `contexto`, o tema é a própria query, como antes. Com `contexto`, o tema é:

```
<query> (contexto: <contexto>)
```

Exemplo: `Documentos (contexto: matrícula na rede municipal 2027)`.

- O mesmo texto vai para o Typesense (`q`) e para o Gemini.
- O rótulo entre parênteses diz ao modelo de busca o que é a pergunta e o que é o pano de fundo.
- Query vazia com contexto usa só o contexto.
- `contexto` vazio ou só com espaços vale ausente.

**Alternativa descartada:** concatenar sem rótulo (`Documentos matrícula 2027`). O modelo não distingue a pergunta do assunto.
**Decidido por:** Claude, sem revisão humana.

### D12. Filtro com contexto

**Decisão:**
- **Eleitoral:** avalia a query e o contexto, e bloqueia se qualquer um for barrado. Senão o contexto viraria um jeito de contornar o filtro. Com whitelist, os dois precisam casar.
- **Sem assunto:** não roda com contexto. "Sim" com o contexto "matrícula 2027" tem assunto.

**Decidido por:** especificação do card.

### D13. Assinatura e docstring da tool

**Decisão:**
- `google_search(query, contexto=None)`. O campo é opcional: o Salesforce chama a tool como hoje.
- A docstring e a descrição dos campos orientam a query como pergunta independente, com o assunto da conversa, sem cumprimento, narrativa nem dado pessoal, em português e curta. Também documentam `contexto`.

Isso é o item A do F4 no lado do MCP, para qualquer cliente. O item A no Salesforce (descrição da action e prompt do subagente) é outro PR.

---

## Registro no BigQuery e no span

**Decisão:** a linha em `brutos_eai_logs.mcp` (`endpoint = /tools/google_search`) passa a ter, em `response`, nos dois caminhos (busca e bloqueio):

| Campo | Conteúdo |
| --- | --- |
| `query` | a query redigida, sem o contexto |
| `contexto` | o contexto redigido, ou `null` |
| `pii_redigida` | lista dos tipos redigidos na query e no contexto, sem repetição (ex.: `["cpf", "endereco"]`) |

- Na resposta do Gemini, `query` era o texto pesquisado. Agora é a query do agente, para continuar comparável com o histórico. Sem contexto, os dois são iguais. Com contexto, o texto pesquisado se reconstrói por D11.
- Na resposta do Typesense, `query` não existia e passa a existir.
- **Span:** `search.query.has_context` (bool), `search.query.pii_redacted` (bool) e `search.query.pii_types` (lista, só quando houve redação). Nunca o valor.

**Decidido por:** especificação do card (o quê); Claude (nomes dos campos e manter `query` sem o contexto).

---

## Limitações conhecidas

- **Nome sem gatilho passa.** Exemplo: "Ana Souza quer saber do IPTU". Resolver exige reconhecimento de entidades (D9).
- **Nome de familiar em minúsculas ou só em maiúsculas passa.** Exemplos: "meu filho joao", "MINHA FILHA ANA". É o custo de não apagar "meu filho tem TEA".
- **Endereço sem tipo de logradouro passa.** Exemplo: "moro na Barata Ribeiro 200".
- **Falso positivo de placa:** uma palavra de três letras maiúsculas seguida de quatro dígitos vira `[placa]` (ex.: "RIO2016").
- **Protocolo com 11 dígitos e dígito verificador válido por acaso** (cerca de 1 em 100) vira `[cpf]`.
- **O interceptor de erros recebe os argumentos da tool como chegaram.** Eles passam pela redação própria dele (CHATR-113/167), que cobre CPF, telefone e e-mail, não endereço nem nome. Como antes deste card.

## Pendências

| Pendência | Responsável | Bloqueia o quê |
| --- | --- | --- |
| Salesforce: `@InvocableVariable` `contexto` em `GoogleSearchAction.cls`, input na action do agente e instrução para preenchê-lo (F4-B) | A definir | Uso do contexto em produção. Sem ele, nada quebra |
| `prefrio`: descrição da action, do campo `query` e do subagente `google_search` (F4-A) | A definir | Nada |
| Validação das listas `expressoes` e `vocabulario` (D4) | A definir | Nada; mudança entra por PR |
| Decidir se CNPJ entra na redação da query (D8) | A definir | Nada |
| Depois do deploy, medir no BigQuery a taxa de `filter_reason = sem_assunto` por regra e a de `pii_redigida` não vazia, e comparar com a medição de 6,3% e 2% | A definir | Nada |
