# Plano de Melhoria: Multi-Step Service (MSS) Framework

## 🎯 Objetivo

Reduzir 50-70% do código boilerplate nos workflows MSS através de:
1. **Node helpers** - Funções utilitárias simples e composáveis
2. **UM decorator opcional** - `@collect_data` (apenas para casos triviais, já inclui error handling)
3. **Audit logs** - Sistema de auditoria opcional (pode ser ignorado)

## 🧭 Filosofia do Design

**Princípios:**
- ✅ **Flexibilidade > Abstração**: Deve ser fácil escrever lógica custom nos nodes
- ✅ **Helpers explícitos**: Funções que você chama quando quer, não magia
- ✅ **Opt-in total**: Se não quiser usar, não usa - código normal funciona
- ✅ **Zero over-engineering**: Sem abstrações que só funcionam em 1 caso específico

**Anti-patterns a evitar:**
- ❌ Múltiplos decorators empilhados (`@handle_errors` + `@collect_data` + `@require_fields`...)
- ❌ Abstrações muito específicas (`collect_field_with_api_call`)
- ❌ "Magia" que esconde o que está acontecendo

---

## 📊 Impacto Esperado

| Workflow | Antes | Depois | Redução |
|----------|-------|--------|---------|
| IPTU | 991 linhas | ~500 linhas | 50% |
| Novos workflows | - | - | 60-70% desde o início |

**Boilerplate eliminado:** ~500-730 linhas repetitivas

---

## 🏗️ Arquitetura da Solução

### 1. Node Helpers (`core/node_helpers.py`)

Funções utilitárias simples que você chama explicitamente:

#### `collect_field()` - Coleta de campo com validação

```python
def collect_field(
    state: ServiceState,
    field_name: str,
    payload_model: Type[BaseModel],
    prompt_message: str | Callable[[ServiceState], str],
    store_in_internal: bool = False,
) -> Any | None:
    """
    Coleta e valida um campo do usuário.

    Returns:
        - O valor do campo se já existe ou foi validado
        - None se precisa pausar para pedir input (state.agent_response já setado)

    Lógica:
        1. Se campo já existe em state.data → retorna valor
        2. Se campo no payload → valida, salva, retorna valor
        3. Se não tem → seta agent_response, retorna None
    """
```

**Uso - Node 100% boilerplate:**
```python
@handle_errors
async def _escolher_ano(self, state: ServiceState) -> ServiceState:
    ano = collect_field(state, "ano_exercicio", EscolhaAnoPayload, "Informe o ano:")
    if ano is None:
        return state  # Pausou para pedir input

    # Opcional: lógica custom aqui se precisar
    logger.info(f"Ano escolhido: {ano}")
    return state
```

**Uso - Node com lógica custom:**
```python
@handle_errors
async def _informar_inscricao(self, state: ServiceState) -> ServiceState:
    # Lógica custom: detecta mudança e reseta
    if "inscricao_imobiliaria" in state.payload:
        validated = InscricaoPayload.model_validate(state.payload)
        if state.data.get("inscricao_imobiliaria") != validated.inscricao_imobiliaria:
            reset_fields(state, ["ano", "guia", "cotas"])

    # Helper para coletar
    inscricao = collect_field(
        state, "inscricao_imobiliaria", InscricaoPayload,
        "Informe a inscrição:"
    )
    if inscricao is None:
        return state

    # Lógica custom: busca dados do imóvel
    try:
        dados = await self.api.get_imovel_info(inscricao)
        state.data["endereco"] = dados["endereco"]
        state.data["proprietario"] = dados["proprietario"]
    except:
        state.data["endereco"] = "N/A"
        state.data["proprietario"] = "N/A"

    return state
```

---

#### `already_collected()` - Verifica se campo existe

```python
def already_collected(state: ServiceState, field_name: str, check_internal: bool = False) -> bool:
    """Verifica se campo já foi coletado."""
    target = state.internal if check_internal else state.data
    return field_name in target and target[field_name] is not None
```

**Uso:**
```python
@handle_errors
async def _consultar_guias(self, state: ServiceState) -> ServiceState:
    # Early exit se já consultou
    if already_collected(state, "dados_guias", check_internal=True):
        return state

    # Consulta API
    dados = await self.api.consultar_guias(...)
    state.internal["dados_guias"] = dados
    return state
```

---

#### `ask_for_field()` - Constrói AgentResponse

```python
def ask_for_field(
    state: ServiceState,
    message: str,
    payload_model: Type[BaseModel],
    error_message: str | None = None,
) -> None:
    """Seta agent_response pedindo campo ao usuário."""
    state.agent_response = AgentResponse(
        description=message,
        payload_schema=payload_model.model_json_schema(),
        error_message=error_message,
    )
```

**Uso:**
```python
if "confirmacao" not in state.payload:
    ask_for_field(state, "Confirma os dados?", ConfirmacaoPayload)
    return state
```

---

#### `reset_fields()` - Remove campos do state

```python
def reset_fields(
    state: ServiceState,
    fields: list[str],
    from_data: bool = True,
    from_internal: bool = False,
) -> None:
    """Remove campos de state.data e/ou state.internal."""
    if from_data:
        for field in fields:
            state.data.pop(field, None)
    if from_internal:
        for field in fields:
            state.internal.pop(field, None)
```

**Uso:**
```python
# Reset quando inscricao muda
if nova_inscricao != inscricao_atual:
    reset_fields(state, ["ano", "guia", "cotas", "dados_guias"])
```

---

#### `validate_required_fields()` - Valida pré-requisitos

```python
def validate_required_fields(
    state: ServiceState,
    required: list[str],
    check_internal: bool = False,
) -> str | None:
    """
    Valida que campos obrigatórios existem.

    Returns:
        - None se todos existem
        - Nome do primeiro campo faltante
    """
    target = state.internal if check_internal else state.data
    for field in required:
        if field not in target or target[field] is None:
            return field
    return None
```

**Uso:**
```python
@handle_errors
async def _gerar_darm(self, state: ServiceState) -> ServiceState:
    # Valida pré-requisitos
    missing = validate_required_fields(state, ["inscricao", "ano", "guia", "cotas"])
    if missing:
        state.agent_response = AgentResponse(
            description=f"Campo {missing} ausente. Reiniciando...",
            payload_schema=InscricaoPayload.model_json_schema()
        )
        return state

    # Lógica de geração
    ...
```

---

**Outros helpers úteis:**
- `respond_and_continue(state)` - Limpa agent_response para continuar
- `save_to_state(state, {field: value, ...})` - Salva múltiplos valores
- `get_from_state(state, field, default=None)` - Get com default

**Localização:** `/Users/m/github/emd/app-mcp-server/src/tools/multi_step_service/core/node_helpers.py`

---

### 2. Decorator Único (Opcional): `@collect_data`

**Quando usar:**
- Node que APENAS coleta um campo
- ZERO lógica custom
- 99% boilerplate, 1% código real

**Quando NÃO usar:**
- Node precisa de validação custom
- Precisa chamar API depois de coletar
- Precisa resetar outros campos
- Precisa de qualquer lógica → **Use helper `collect_field()` em vez do decorator**

```python
def collect_data(
    field_name: str,
    payload_model: Type[BaseModel],
    prompt_message: str | Callable[[ServiceState], str],
    store_in_internal: bool = False,
):
    """
    Decorator para coleta automática de campo.

    Já inclui @handle_errors internamente - NÃO precisa empilhar!

    Use APENAS para nodes triviais sem lógica custom.
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(instance, state: ServiceState) -> ServiceState:
            try:
                # Usa collect_field helper internamente
                value = collect_field(state, field_name, payload_model, prompt_message, store_in_internal)

                if value is None:
                    return state  # Pausou

                # Executa node (geralmente vazio ou só logging)
                return await func(instance, state)

            except Exception as e:
                # Error handling built-in
                logger.error(f"Error in {func.__name__}: {e}")
                state.agent_response = state.agent_response or AgentResponse()
                state.agent_response.error_message = str(e)
                state.status = "error"
                return state

        return wrapper
    return decorator
```

**Uso:**
```python
# ANTES (37 linhas):
@handle_errors
async def _escolher_ano_exercicio(self, state: ServiceState) -> ServiceState:
    inscricao = state.data.get("inscricao_imobiliaria", "N/A")
    endereco = state.data.get("endereco", "N/A")
    proprietario = state.data.get("proprietario", "N/A")

    if "ano_exercicio" in state.payload:
        try:
            validated = EscolhaAnoPayload.model_validate(state.payload)
            state.data["ano_exercicio"] = validated.ano_exercicio
            state.agent_response = None
            return state
        except Exception as e:
            state.agent_response = AgentResponse(
                description=Templates.escolher_ano(...),
                payload_schema=EscolhaAnoPayload.model_json_schema(),
                error_message=str(e)
            )
            return state

    if "ano_exercicio" in state.data:
        state.agent_response = None
        return state

    state.agent_response = AgentResponse(
        description=Templates.escolher_ano(...),
        payload_schema=EscolhaAnoPayload.model_json_schema()
    )
    return state

# DEPOIS (7 linhas):
@collect_data(
    "ano_exercicio",
    EscolhaAnoPayload,
    lambda s: Templates.escolher_ano(
        s.data.get("inscricao_imobiliaria", "N/A"),
        s.data.get("endereco", "N/A"),
        s.data.get("proprietario", "N/A")
    )
)
async def _escolher_ano_exercicio(self, state: ServiceState) -> ServiceState:
    # Opcional: logging
    return state
```

**Importante:**
- ❌ **NÃO empilhar** `@handle_errors` com `@collect_data` (já incluso)
- ✅ **Use helper** `collect_field()` se precisar de lógica custom

**Localização:** `/Users/m/github/emd/app-mcp-server/src/tools/multi_step_service/core/decorators.py`

---

### 3. Audit Logs (`core/audit.py`) - Opcional

Sistema de auditoria não-intrusivo. **Pode ser totalmente ignorado se não precisar.**

**Features:**
- Opt-in via `MSS_ENABLE_AUDIT=true`
- Storage: file-based ou Redis
- Eventos: node_enter, node_exit, api_call, validation_error, etc
- Async logging (non-blocking)

**Uso:**
```python
# Habilitar globalmente (opcional)
from src.tools.multi_step_service.core.audit import enable_audit_logging
enable_audit_logging(storage_type="file")

# Decorator para nodes críticos (opcional)
@handle_errors
@audit_node("api_call", capture_fields=["inscricao", "ano"])
async def _consultar_guias(self, state: ServiceState) -> ServiceState:
    # Automaticamente logado
    pass
```

**Localização:** `/Users/m/github/emd/app-mcp-server/src/tools/multi_step_service/core/audit.py`

---

## 📝 Exemplos Práticos: Before/After

### Exemplo 1: Node Trivial (100% boilerplate)

**ANTES (37 linhas):**
```python
@handle_errors
async def _escolher_ano_exercicio(self, state: ServiceState) -> ServiceState:
    """Coleta o ano de exercício para consulta do IPTU."""
    inscricao = state.data.get("inscricao_imobiliaria", "N/A")
    endereco = state.data.get("endereco", "N/A")
    proprietario = state.data.get("proprietario", "N/A")

    if "ano_exercicio" in state.payload:
        try:
            validated_data = EscolhaAnoPayload.model_validate(state.payload)
            state.data["ano_exercicio"] = validated_data.ano_exercicio
            state.agent_response = None
            return state
        except Exception as e:
            state.agent_response = AgentResponse(
                description=IPTUMessageTemplates.escolher_ano(
                    inscricao=inscricao, endereco=endereco, proprietario=proprietario
                ),
                payload_schema=EscolhaAnoPayload.model_json_schema(),
                error_message=f"Ano inválido: {str(e)}",
            )
            return state

    if "ano_exercicio" in state.data:
        state.agent_response = None
        return state

    response = AgentResponse(
        description=IPTUMessageTemplates.escolher_ano(
            inscricao=inscricao, endereco=endereco, proprietario=proprietario
        ),
        payload_schema=EscolhaAnoPayload.model_json_schema(),
    )
    state.agent_response = response
    return state
```

**OPÇÃO A - Usando decorator (7 linhas):**
```python
@collect_data(
    "ano_exercicio",
    EscolhaAnoPayload,
    lambda s: IPTUMessageTemplates.escolher_ano(
        s.data.get("inscricao_imobiliaria", "N/A"),
        s.data.get("endereco", "N/A"),
        s.data.get("proprietario", "N/A")
    )
)
async def _escolher_ano_exercicio(self, state: ServiceState) -> ServiceState:
    return state
```

**OPÇÃO B - Usando helper (10 linhas):**
```python
@handle_errors
async def _escolher_ano_exercicio(self, state: ServiceState) -> ServiceState:
    """Coleta o ano de exercício para consulta do IPTU."""
    ano = collect_field(
        state, "ano_exercicio", EscolhaAnoPayload,
        lambda s: IPTUMessageTemplates.escolher_ano(
            s.data.get("inscricao_imobiliaria", "N/A"),
            s.data.get("endereco", "N/A"),
            s.data.get("proprietario", "N/A")
        )
    )
    if ano is None:
        return state

    return state
```

**Economia: 37 → 7-10 linhas (73-78%)**

---

### Exemplo 2: Node com Lógica Custom (coleta + API + reset)

**ANTES (66 linhas):**
```python
@handle_errors
async def _informar_inscricao_imobiliaria(self, state: ServiceState) -> ServiceState:
    """Coleta a inscrição imobiliária do usuário."""
    if "inscricao_imobiliaria" in state.payload:
        try:
            validated_data = InscricaoImobiliariaPayload.model_validate(state.payload)
            nova_inscricao = validated_data.inscricao_imobiliaria
            inscricao_atual = state.data.get("inscricao_imobiliaria")

            # Reset se mudou
            if inscricao_atual and nova_inscricao != inscricao_atual:
                state_helpers.reset_completo(state)

            state.data["inscricao_imobiliaria"] = nova_inscricao
            logger.info(f"✅ Inscrição salva: {nova_inscricao}")

            # Busca dados do imóvel
            try:
                dados_imovel = await self.api_service.get_imovel_info(inscricao=nova_inscricao)
                if dados_imovel:
                    state.data["endereco"] = dados_imovel["endereco"]
                    state.data["proprietario"] = dados_imovel["proprietario"]
            except (APIUnavailableError, AuthenticationError) as e:
                logger.warning(f"Não foi possível carregar dados do imóvel: {str(e)}")
                state.data["endereco"] = "Não disponível"
                state.data["proprietario"] = "Não disponível"

            state.agent_response = None
            return state

        except Exception as e:
            response = AgentResponse(
                description=IPTUMessageTemplates.solicitar_inscricao(),
                payload_schema=InscricaoImobiliariaPayload.model_json_schema(),
                error_message=f"Inscrição imobiliária inválida: {str(e)}",
            )
            state.agent_response = response
            return state

    if "inscricao_imobiliaria" in state.data:
        return state

    response = AgentResponse(
        description=IPTUMessageTemplates.solicitar_inscricao(),
        payload_schema=InscricaoImobiliariaPayload.model_json_schema(),
    )
    state.agent_response = response
    return state
```

**DEPOIS - Usando helper (28 linhas):**
```python
@handle_errors
async def _informar_inscricao_imobiliaria(self, state: ServiceState) -> ServiceState:
    """Coleta a inscrição imobiliária do usuário."""

    # Detecta mudança e reseta
    if "inscricao_imobiliaria" in state.payload:
        validated = InscricaoImobiliariaPayload.model_validate(state.payload)
        nova = validated.inscricao_imobiliaria
        atual = state.data.get("inscricao_imobiliaria")

        if atual and nova != atual:
            reset_fields(state, ["endereco", "proprietario", "ano_exercicio", "dados_guias"])

    # Coleta inscrição
    inscricao = collect_field(
        state, "inscricao_imobiliaria", InscricaoImobiliariaPayload,
        IPTUMessageTemplates.solicitar_inscricao()
    )
    if inscricao is None:
        return state

    # Busca dados do imóvel
    try:
        dados = await self.api_service.get_imovel_info(inscricao)
        state.data["endereco"] = dados["endereco"]
        state.data["proprietario"] = dados["proprietario"]
    except:
        state.data["endereco"] = "N/A"
        state.data["proprietario"] = "N/A"

    return state
```

**Economia: 66 → 28 linhas (58%)**
**Mantém: Total flexibilidade para lógica custom**

---

### Exemplo 3: Node Complexo (mantém código normal)

**Para nodes muito complexos, você escreve normalmente:**
```python
@handle_errors
async def _consultar_guias_disponiveis(self, state: ServiceState) -> ServiceState:
    """Node complexo com lógica de negócio pesada."""

    # Usa apenas helpers pontuais
    if already_collected(state, "dados_guias", check_internal=True):
        return state

    missing = validate_required_fields(state, ["inscricao_imobiliaria", "ano_exercicio"])
    if missing:
        ask_for_field(state, f"Campo {missing} ausente", InscricaoPayload)
        return state

    # Resto do código: lógica complexa custom
    inscricao = state.data["inscricao_imobiliaria"]
    ano = state.data["ano_exercicio"]

    try:
        dados_guias = await self.api_service.consultar_guias(inscricao, ano)
    except DataNotFoundError:
        # Lógica complexa de fallback
        ...

    # ... mais lógica ...

    return state
```

**Flexibilidade total mantida!**

---

## 🗂️ Estrutura de Arquivos

### Arquivos Novos
```
src/tools/multi_step_service/
├── core/
│   ├── node_helpers.py (NOVO - ~200 linhas)
│   ├── decorators.py (NOVO - ~100 linhas - apenas @collect_data)
│   ├── audit.py (NOVO - ~250 linhas, opcional)
│   └── __init__.py (MODIFICAR - adicionar exports)
└── docs/ (NOVO - opcional)
    ├── HELPERS_GUIDE.md
    └── MIGRATION_EXAMPLES.md
```

### Arquivos a Modificar
```
src/tools/multi_step_service/
├── core/__init__.py (adicionar exports)
└── workflows/
    └── iptu_pagamento/
        └── iptu_workflow.py (migrar ~5-7 nodes - 991 → ~500 linhas)
```

---

## 🚀 Estratégia de Implementação

### Fase 1: Fundação (1-2 dias)
**Objetivo:** Criar helpers sem quebrar código existente

**Tarefas:**
1. Criar `core/node_helpers.py`:
   - `collect_field()`
   - `already_collected()`
   - `ask_for_field()`
   - `reset_fields()`
   - `validate_required_fields()`
   - Outros helpers utilitários

2. Criar `core/decorators.py`:
   - APENAS `@collect_data` (já inclui error handling)

3. (Opcional) Criar `core/audit.py`

4. Atualizar `core/__init__.py` com exports

5. **Validar**: Rodar todos os testes - nada deve quebrar

**Arquivos:**
- `/Users/m/github/emd/app-mcp-server/src/tools/multi_step_service/core/node_helpers.py`
- `/Users/m/github/emd/app-mcp-server/src/tools/multi_step_service/core/decorators.py`
- `/Users/m/github/emd/app-mcp-server/src/tools/multi_step_service/core/__init__.py`

---

### Fase 2: Pilot Migration (1 dia)
**Objetivo:** Migrar 1 node simples para validar abordagem

**Target:** `_escolher_ano_exercicio` (node mais trivial)

**Passos:**
1. Migrar usando `@collect_data` decorator
2. Testar workflow IPTU end-to-end
3. Verificar comportamento idêntico
4. Documentar: 37 → 7 linhas (81%)

**Arquivo:**
- `/Users/m/github/emd/app-mcp-server/src/tools/multi_step_service/workflows/iptu_pagamento/iptu_workflow.py`

---

### Fase 3: IPTU Migration (2-3 dias)
**Objetivo:** Migrar nodes elegíveis do IPTU

**Prioridades:**

1. **Nodes triviais** (usar `@collect_data`):
   - `_escolher_ano_exercicio` ✓ (Fase 2)
   - `_usuario_escolhe_guias_iptu`
   - `_usuario_escolhe_cotas_iptu`
   - `_perguntar_formato_darm`

2. **Nodes com lógica** (usar helpers `collect_field`, `reset_fields`):
   - `_informar_inscricao_imobiliaria`
   - `_confirmacao_dados_pagamento`

3. **Nodes complexos** (usar helpers pontuais):
   - `_consultar_guias_disponiveis` (apenas `already_collected`, `validate_required_fields`)
   - `_gerar_darm` (manter maior parte do código original)

**Resultado esperado:**
- IPTU: 991 → ~500 linhas (50%)
- Testes passam
- Comportamento idêntico

---

### Fase 4: Documentação (1 dia - opcional)
**Objetivo:** Documentar para adoção futura

**Criar:**
1. **HELPERS_GUIDE.md**
   - Quando usar helper vs decorator
   - Exemplos práticos
   - Before/after do IPTU

2. **MIGRATION_EXAMPLES.md**
   - Padrões de migração
   - Casos especiais

---

## ✅ Critérios de Sucesso

- [ ] Todos os testes passam (zero breaking changes)
- [ ] IPTU workflow reduzido em 50% (991 → ~500 linhas)
- [ ] Novos workflows podem usar helpers desde o início
- [ ] Backward compatibility 100% (workflows antigos funcionam)
- [ ] Flexibilidade mantida (fácil escrever lógica custom)

---

## 🔑 Arquivos Críticos

### Top 3 para implementação:

1. **`core/node_helpers.py`** (NOVO - ~200 linhas)
   - Fundação de tudo
   - Helpers reutilizáveis simples
   - Maior impacto na redução de boilerplate

2. **`core/decorators.py`** (NOVO - ~100 linhas)
   - APENAS `@collect_data` (já com error handling)
   - Para casos triviais

3. **`workflows/iptu_pagamento/iptu_workflow.py`** (MODIFICAR)
   - Prova de conceito
   - 991 → ~500 linhas

---

## 📈 Métricas de Impacto

### Redução de Código

| Padrão | Ocorrências | Linhas/Cada | Total Economizado |
|--------|-------------|-------------|-------------------|
| Data collection | 15 | 15-30 | 225-450 linhas |
| "Already exists" checks | 15 | 2 | 30 linhas |
| AgentResponse construction | 20 | 5 | 100 linhas |
| Reset logic | 10 | 5-10 | 50-100 linhas |
| **TOTAL** | - | - | **~405-680 linhas** |

### Velocidade de Desenvolvimento

| Métrica | Antes | Depois | Melhoria |
|---------|-------|--------|----------|
| Node trivial | 30-40 linhas | 7-10 linhas | 70-75% redução |
| Node com lógica | 60-80 linhas | 25-35 linhas | 55-60% redução |
| Flexibilidade | Alta | Alta | Mantida |
| Curva de aprendizado | Média | Baixa | Helper = função normal |

---

## ⚠️ Garantias

### Mantém:
✅ LangGraph como engine
✅ StateManager intacto
✅ `@handle_errors` (pode usar separado ou embutido em `@collect_data`)
✅ Flexibilidade total para lógica custom
✅ Backward compatibility 100%

### Remove:
❌ Boilerplate repetitivo
❌ Empilhamento de decorators
❌ Abstrações over-engineered
❌ "Magia" que esconde lógica

---

## 🎯 TL;DR - Resumo Executivo

**O que muda:**
1. **Helpers simples** que você chama quando quer (não magia)
2. **UM decorator opcional** `@collect_data` (apenas para nodes triviais)
3. Total flexibilidade para escrever lógica custom

**O que NÃO muda:**
- LangGraph continua sendo o engine
- Pode escrever código normal quando quiser
- Zero breaking changes

**Economia esperada:**
- Nodes triviais: 70-75% menos código
- Nodes com lógica: 55-60% menos código
- Nodes complexos: 10-20% menos código (helpers pontuais)
