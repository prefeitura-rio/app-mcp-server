# Testes do MCP

Esta pasta concentra os testes automatizados do projeto.

## Estrutura

- `src/tests/unit/`: testes unitários e de integração leve
- `src/tests/hurl/`: smoke HTTP contra a imagem de produção rodando
- `src/tests/e2e/`: testes E2E usados no preview de `staging`
- `src/tests/conftest.py`: defaults de ambiente compartilhados pela suíte

## Como pensar na suíte

Hoje a estratégia de testes está dividida assim:

- `unit`: valida lógica de aplicação, wrappers, middleware, interceptors e workflows com dependências controladas;
- `hurl`: valida que o container sobe e fala HTTP — health, autenticação e handshake MCP;
- `e2e`: valida o preview real no cluster antes da promoção em `staging`.

Ou seja:

- unit pega regressão cedo e barato;
- hurl pega o que só aparece pela porta HTTP, ainda no PR;
- e2e protege o deploy real antes de mexer no serviço estável.

A camada `hurl` existe porque o `pr-quality-gate` construía a imagem, escaneava
com Trivy e a descartava **sem nunca executá-la**. Os unitários montam o app
ASGI em processo (`unit/app/test_app_factory.py`) e leem as rotas, mas não
fazem request algum — então preflight quebrado, `CMD` errado, env var
obrigatória faltando ou auth desligada do `/mcp` passavam o gate verde e só
apareciam em staging, depois do merge.

Uma consequência a ter em mente: **teste Hurl não conta para o gate de
cobertura**, que mede código de produção exercitado pelo pytest. Ele é camada
adicional, não substituta — mover um teste de pytest para Hurl derrubaria o
número e reprovaria o PR pela tolerância de 0,1 pp.

## Comandos úteis

### Rodar todos os testes unitários

```bash
uv run pytest src/tests/unit -q
```

### Rodar a suíte inteira com coverage

```bash
uv run pytest --cov=src --cov-report=term --cov-report=xml -q
```

### Rodar um grupo específico

```bash
uv run pytest src/tests/unit/workflows -q
uv run pytest src/tests/unit/interceptor -q
uv run pytest src/tests/unit/tools -q
```

### Rodar o smoke HTTP localmente

```bash
hurl --test --variable base_url=http://127.0.0.1:8080 --variable token="$TOKEN" \
  src/tests/hurl/smoke.hurl
```

Como subir o container antes disso está em `src/tests/hurl/README.md`.

### Rodar o E2E localmente

```bash
python3 src/tests/e2e/run_preview_e2e.py
```

Detalhes do runner E2E estão em:

- `src/tests/e2e/README.md`

## Coverage

O projeto usa baseline versionada em:

- `.github/coverage-baseline.json`

E o coverage é calibrado para refletir mais o código de aplicação do que arquivos auxiliares. Por isso, alguns caminhos são omitidos via `pyproject.toml`.

## Convenções

- prefira adicionar testes novos perto do domínio afetado;
- evite criar muitos arquivos minúsculos sem necessidade;
- quando um fluxo crítico muda, tente cobrir o comportamento de negócio, não só helper interno;
- em dúvidas, priorize testes unitários antes de expandir o E2E.

## Quando atualizar a documentação

Vale atualizar este README quando houver mudança em:

- estrutura das pastas de teste;
- comandos principais da suíte;
- policy de coverage;
- comportamento do E2E de staging;
- escopo do smoke HTTP (`src/tests/hurl/`).
