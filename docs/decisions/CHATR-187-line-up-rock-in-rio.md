# Line-up do Rock in Rio: por que a tool não entrega horários

**Data:** 28 de agosto de 2026
**Ramo:** `feat/CHATR-187-rock-in-rio-lineup`
**Contexto:** A prefeitura é responsável por informar a agenda de shows do Rock in Rio 2026 (04 a 13 de setembro, Cidade do Rock) pelo chatbot.

---

## Resumo

A tool `rock_in_rio_lineup` entrega **dia + palco + artista** das 156 atrações dos sete dias. Não entrega horário de show, porque a fonte disponível não publica horário. Este documento registra por que a fonte é essa, o que foi descartado e o que precisa mudar para o escopo aumentar.

---

## A fonte

A produção do evento foi contatada e informou que **não tem condições de disponibilizar a grade em planilha ou qualquer outro formato**. A orientação foi encaminhar o cidadão para o app oficial. Sobrou o site.

O site é WordPress 6.9, e a REST API está bloqueada: qualquer rota sob `/wp-json/` devolve o HTML da home em vez de JSON — inclusive as rotas dos post types próprios (`wp/v2/dia/<id>`) que o próprio tema referencia, e o `sitemap_index.xml`. Não há endpoint de agenda.

O que viabiliza o caso é que as páginas de line-up são renderizadas no servidor:

| Recurso | URL |
|---|---|
| Dia | `https://rockinrio.com/rio/line-up/dia/{DD}-set/` |
| Palco | `https://rockinrio.com/rio/pt-br/line-up/palco/{slug}/` |
| Artista | `https://rockinrio.com/rio/pt-br/line-up/{slug}/` |

O HTML chega completo, sem depender de JavaScript. Por isso o scraper usa `httpx` puro — **sem browser headless**. É também por isso que o `crawl4ai`, removido no CHATR-177, não precisou voltar.

A estrutura é: cabeçalho de palco em `<div class="data"><span>Palco X</span></div>`, seguido dos `<div class="bloco-artista">` que pertencem a ele. O agrupamento é **por ordem no documento**, não por aninhamento — o cabeçalho é irmão dos blocos, não pai. Essa é a premissa central do parser, e `parse_dia` levanta `LineupInvalido` se um artista aparecer antes de qualquer cabeçalho.

## Por que não há horários

O site não os publica. A evidência está no próprio markup: o `<span>` que guardaria a hora vem **vazio e dentro de um comentário HTML** em todos os blocos de atração:

```html
<!--div class="flag" ...><span class="flag-data"><span></span>  • Palco Mundo</span><div -->
```

A página de cada artista traz apenas o dia (`04.SET`). A grade horária foi divulgada por volta de 26/08/2026, mas existe somente no **app oficial** — o próprio site anuncia isso: *"O aplicativo Rock in Rio também liberou a agenda e agora você já pode montar sua própria programação"*.

Consequências diretas no produto:

- "Quando é o show da banda X" se resolve como **dia**, nunca como hora.
- **Montar cronograma com detecção de conflito de horário é impossível** a partir desta fonte.

### O risco que isso cria

O risco número um da tool é o modelo **inventar** horário: "às 22h no Palco Mundo" é uma frase que sai natural de um LLM. Por isso a ausência é declarada de forma redundante — na descrição publicada no catálogo MCP (antes da chamada), no bloco `horarios` e nas `instrucoes_de_resposta` do retorno.

## Alternativas descartadas

| Alternativa | Por que não |
|---|---|
| API do app oficial | Exigiria engenharia reversa do app, com risco de auth/pinning e prazo de 4 dias até o festival. Consumir a API do app da produção sem alinhamento prévio é decisão que não cabia tomar de esguelha. |
| Grade horária da imprensa | Traria horários, mas congelados na data de publicação e sem origem oficial. Dado de terceiro sobre horário é pior que ausência de horário: erra e ninguém sabe quando. |
| Snapshot commitado como fallback | Ver abaixo. |

## A regra que desenha o cache

**Preferimos não entregar a entregar dado desatualizado.** Mandar o cidadão para o palco errado, ou para o dia errado, é pior do que dizer que a consulta falhou.

Disso decorre a arquitetura inteira de `cache.py`:

| Decisão | Escolha |
|---|---|
| Teto de idade | 60 min, duro. Nada mais velho sai — sai exceção |
| Atualização | Laço em background a cada 15 min |
| Prefetch | No `lifespan`, aguardado |
| Níveis | Memória do processo na frente, Redis atrás |
| Degradação | site → cache dentro do teto → falha explícita |
| Busca | Tudo ou nada: um dia que não baixa invalida a grade inteira |

Dois pontos merecem registro por não serem óbvios:

**Por que o intervalo de refresh é menor que o teto.** Se a revalidação só acontecesse no vencimento, o dado expiraria e já estaria velho demais no mesmo instante — a tolerância a uma queda curta do site nasceria zerada. Revalidar aos 15 min é o que transforma o teto de 60 min em teto de verdade, com folga para o site ficar fora do ar por até três ciclos.

**Por que não há snapshot commitado.** Foi considerado e descartado: um arquivo versionado semanas antes é exatamente o dado desatualizado que a regra proíbe. O último recurso da cadeia é falhar, não servir arquivo antigo.

**Por que a busca é tudo ou nada.** Uma grade parcial é o pior desfecho possível — o chatbot afirmaria com convicção que uma banda não está no festival só porque a página dela não respondeu.

## Semântica de datas

Duas armadilhas, ambas com teste próprio em `test_rock_in_rio_tool.py`:

1. **A jornada avança pela madrugada.** Às 2h de sábado, o que está acontecendo é a programação de sexta. Antes das 6h (`HORA_FIM_DA_JORNADA`), a jornada de referência é o dia anterior. Sem isso, quem pergunta de dentro da Cidade do Rock de madrugada — o público mais provável naquele horário — recebe a resposta errada.
2. **O festival tem intervalo.** Nos dias 08, 09 e 10 de setembro ele não terminou, mas não há show. Por isso `status` (`antes_do_festival` / `durante_o_festival` / `encerrado`) e `hoje_tem_show` são campos separados, e a data de calendário acompanha sempre o veredito.

Depois de 14/09 às 6h, a tool responde `encerrado`. Ela não precisa ser removida às pressas.

## Alarme de mudança do site

São três camadas, porque nenhuma delas sozinha resolve.

**No parser, em produção.** `parse_dia` recusa a página em vez de devolver uma grade parcial: confronta o número de blocos de artista presentes no HTML com o número que conseguiu ler, exige um piso de atrações por dia (`MIN_ATRACOES_POR_DIA`) e recusa nome acima de `MAX_TAMANHO_NOME`. Sem isso, os dois modos de falha mais prováveis do site — uma `<section>` nova no meio da lista e um bloco de artista que mudou de forma — cortavam o dia pela metade em silêncio, e o chatbot passava a negar bandas que estão no festival. Grade parcial é o pior desfecho possível, então ela vira `LineupInvalido`, que o cache converte em indisponibilidade explícita.

**Nos testes unitários.** As fixtures são os sete dias inteiros, e não uma amostra: foi assim que apareceu o `<span>` de nota de rodapé no nome da MEDUZA, que só existe no dia 06 e que um parser ancorado no ícone decorativo `<i>` engolia junto com o nome. Elas continuariam verdes se o site mudasse de tema — é o limite desta camada, e o motivo de existir a próxima.

**Contra o site real.** O runner `src/tests/e2e/run_rock_in_rio_contract.py` valida faixas de atrações e o catálogo de palcos na fonte. Fica fora do CI de propósito: uma indisponibilidade momentânea do rockinrio.com não pode reprovar um PR alheio.

```bash
uv run python src/tests/e2e/run_rock_in_rio_contract.py
```

E o `src/tests/e2e/run_chat_rock_in_rio.py` responde a outra metade da pergunta — não "o parser ainda casa?", mas "o que o cidadão recebe?".

## O que muda se a produção liberar a grade horária

O ponto de troca é `scraper.py`, não o resto. Concretamente:

1. Acrescentar o campo de horário ao dataclass `Show` — hoje ele existe deliberadamente sem esse campo.
2. Trocar a fonte em `buscar_lineup`.
3. Remover os avisos de ausência de horário em `tool.py` (`_AVISO_SEM_HORARIOS` e o parágrafo final de `descricao_da_tool`).
4. Só então passa a fazer sentido reabrir o "montar cronograma com detecção de conflito", que foi o pedido original e que esta fonte não permite atender.

O teto de 60 min de idade fica ainda mais importante nesse cenário: mudança de horário de última hora é justamente o dado que não pode chegar velho.
