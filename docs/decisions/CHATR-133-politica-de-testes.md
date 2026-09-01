# Política de versionamento e camadas de teste

**Status:** implementado
**Data:** 2026-09-01
**Tipo:** Débito técnico — CHATR-133 (sub-tasks CHATR-165, CHATR-166)

Registra três decisões que até aqui só existiam implícitas no YAML do CI, na
descrição de tickets do Jira ou em comentários espalhados: **quais camadas de
teste existem e o que decide entre elas**, **por que testcontainers não foi
adotado**, e **por que a regra do `.gitignore` é do jeito que é**.

---

## Contexto

O CHATR-133 nasceu de um acidente. O `.gitignore` ignorava `test*.py` sem
âncora, então o padrão casava em qualquer nível da árvore e engolia a suíte
inteira em `src/tests/`. O efeito era silencioso: quem escrevia um teste novo
via a suíte passar localmente, fazia `git add .`, e o arquivo não entrava no
commit — sem aviso, sem aparecer no `git status`.

Foi detectado no CHATR-119, quando se descobriu que os 6 arquivos de teste de
`src/tests/unit/health/` nunca tinham chegado ao repositório. `src/health/*`
constava com **0% de cobertura na CI** enquanto os testes existiam apenas na
máquina de quem os escreveu.

| | Testes na CI | Cobertura |
| --- | --- | --- |
| Antes | 127 | 65% |
| Depois | 195 | 71% |

A correção do `.gitignore` resolveu o sintoma. O que faltava era a política:
decidir e escrever o que entra no repo, onde cada tipo de teste mora e o que
roda no gate — antes que a próxima camada entrasse de forma improvisada.

---

## Decisão 1 — Quatro camadas, com fronteira explícita

A suíte tem quatro camadas. Cada uma existe porque pega uma classe de falha que
as outras não pegam.

| Camada | Diretório | O que prova | Conta para cobertura |
| --- | --- | --- | --- |
| `unit` | [src/tests/unit/](../../src/tests/unit/) | lógica com dependências controladas | sim |
| `integration` | [src/tests/integration/](../../src/tests/integration/) | o que só o servidor real prova | sim |
| `hurl` | [src/tests/hurl/](../../src/tests/hurl/) | o artefato sobe e fala HTTP | **não** |
| `e2e` | [src/tests/e2e/](../../src/tests/e2e/) | o preview real antes da promoção | não |

O critério de roteamento entre `unit` e `integration` é estreito de propósito e
está detalhado em [src/tests/README.md](../../src/tests/README.md): vai para
`integration/` o que o dublê **não consegue provar** — TTL que chega ao
servidor, `LTRIM` que corta de fato, `SET NX` que serializa concorrência.
Verificar *quando* grava e *com que chave* continua sendo trabalho de unitário,
que é mais rápido e não depende de infra.

**Hurl não conta para o gate de cobertura**, que mede código de produção
exercitado pelo pytest. É camada adicional, não substituta: mover um teste de
pytest para Hurl derruba o número e reprova o PR pela tolerância de 0,1 pp.

A camada `hurl` (CHATR-165) existe porque o `pr-quality-gate` construía a
imagem, escaneava com Trivy e a descartava **sem nunca executá-la** — preflight
quebrado, `CMD` errado, env var obrigatória faltando ou auth desligada do
`/mcp` passavam o gate verde e só apareciam em staging, depois do merge.

## Decisão 2 — Integração roda no mesmo job do gate, não em workflow próprio

Os testes de integração rodam no job `test` do
[pr-quality-gate.yaml](../../.github/workflows/pr-quality-gate.yaml), junto com
os unitários, e não em workflow separado.

**Por quê.** O `redis:7-alpine` sobe como *service container* do GitHub
Actions: o runner puxa a imagem e roda o healthcheck **fora** dos steps do job,
então o custo não aparece no tempo de execução do pytest. O
`testpaths = ["src/tests"]` do [pyproject.toml](../../pyproject.toml) varre
tudo de uma vez, e a cobertura sai de uma medição só — separar em dois jobs
exigiria combinar dois `coverage.xml` para o gate de baseline continuar
significando a mesma coisa.

**Degradação graciosa.** Sem Redis alcançável em `REDIS_URL`, cada módulo de
integração se pula inteiro via `skipif`. Isso mantém `uv run pytest` verde em
máquina de dev sem infra — com a contrapartida, registrada no README, de que
**passar não significa ter rodado**.

## Decisão 3 — Testcontainers: não adotar agora (CHATR-166)

Mantida a abordagem atual — *service container* do Actions + `skipif`. Quatro
razões:

1. **O problema já está resolvido, e de graça.** O gate já sobe o Redis fora
   dos steps do job; testcontainers pagaria esse custo dentro da execução do
   pytest. Seria trocar uma solução mais rápida por uma mais lenta.

2. **A superfície que ganharia é pequena.** Medido em 01/09/2026:

   | Métrica | Valor |
   | --- | --- |
   | Testes na suíte | 1108 |
   | Testes que tocam infra real | 23 (2 módulos em `integration/`) |
   | Usos de mock/monkeypatch | ~1478 |

   O resto é lógica com dublês. Container nenhum melhora isso.

3. **Migrar quebraria o fluxo local.** Em máquina onde o daemon do Docker está
   desligado mas o Redis roda nativo, os testes de integração **passam hoje**.
   Com testcontainers passariam a exigir Docker Desktop de pé — troca de um
   `skipif` educado por uma dependência pesada, sem ganho de cobertura.

4. **O resto das dependências externas não é containerizável de forma útil.**
   BigQuery e GCS são Google, sem emulador de primeira classe no
   `testcontainers-python`. As ~12 APIs HTTP (IPTU, Dívida Ativa, SGRC,
   Nominatim, Gmaps…) pediriam WireMock ou `respx`, não um container de banco.

### Gatilho de reavaliação

Existe um caso onde testcontainers ganha de verdade. *Service containers* do
Actions **não aceitam argumentos de comando** — só `image`, `env`, `ports`,
`volumes`, `options` — então não dá para subir
`redis-server --maxmemory 2mb --maxmemory-policy noeviction`, nem derrubar o
servidor no meio do teste. É exatamente o que falta para fechar dois pontos em
aberto:

- [redis-maxmemory-policy.md](redis-maxmemory-policy.md), cuja decisão segue
  **pendente**. O próprio doc aponta que `save_user_data`
  ([state.py:151-154](../../src/tools/multi_step_service/core/state.py#L151-L154))
  não tem `try/except` e estouraria com `OOM command not allowed` sob
  `noeviction`.
  Nada na suíte prova isso hoje.
- Caminhos de degradação com o Redis fora: fallback da DLQ para `.jsonl`
  ([bigquery.py](../../src/utils/bigquery.py)), `CompositeBackend` caindo para
  o backend JSON, `health_check()` retornando `False`. Hoje só são exercitados
  contra mocks que levantam exceção na hora certa — e um mock não reproduz
  socket que expira no meio do comando.

**Reabrir esta decisão quando** alguém for escrever os testes de eviction ou de
queda do Redis. Antes disso, não há teste que justifique a dependência.

### Se/quando adotar — notas de implementação

- `testcontainers[redis]` no grupo `dev` do `pyproject.toml`, nunca em
  `dependencies`.
- Camada opcional e separada: marcador próprio (ex. `integration_docker`), com
  skip automático quando não houver Docker.
- ⚠️ O `addopts` tem `--strict-markers` e **não existe chave `markers`** no
  `pyproject.toml`. Um marcador novo não registrado quebra a suíte inteira.

## Decisão 4 — Regra do `.gitignore`: padrões de teste ancorados na raiz

Os padrões de rascunho ficam ancorados em `/`:

```
/test*.py
/test*.ipynb
/test*.md
/test*.json
```

A barra inicial é o ponto todo. Sem ela o padrão casa em qualquer nível e volta
a engolir `src/tests/` — o modo de falha descrito no contexto acima. **Não
voltar a usar `test*.py` nem `**/**/test*.py`.**

Fora do controle de versão fica só o que é gerado: `.coverage`, `coverage.xml`,
`pytest-report.xml`, `htmlcov/` e os relatórios HTML do Hurl, via
[src/tests/hurl/store/.gitignore](../../src/tests/hurl/store/.gitignore)
(`*` + `!.gitignore`). Os `.hurl` são versionados.

A mesma armadilha existia em `docs/*` e foi corrigida com a exceção
`!docs/decisions/` — sem ela, este próprio arquivo só entraria no repo com
`git add -f`.

**Critério de aceite**, verificado com `git check-ignore -v`:

| Caminho | Resultado |
| --- | --- |
| `src/tests/unit/test_novo.py` | não ignorado |
| `src/tests/integration/test_novo.py` | não ignorado |
| `src/tests/hurl/api.hurl` | não ignorado |
| `docs/decisions/*.md` | não ignorado |
| `test_rascunho.py` (raiz) | ignorado — comportamento desejado |

---

## Consequências

- Um arquivo de teste novo, em qualquer das quatro camadas, entra com `git add`
  normal — sem `-f` e sem passar despercebido.
- O gate continua sendo uma medição de cobertura só, num job só.
- Testes que precisam de Redis são silenciosamente pulados fora do CI. Quem
  mexe nesses caminhos precisa de um Redis de pé para saber o que está testando.
- Enquanto o gatilho da Decisão 3 não for puxado, os caminhos de degradação do
  Redis seguem cobertos só por mocks — limitação conhecida e aceita.
