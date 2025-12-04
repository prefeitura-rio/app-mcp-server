# Multi-Step Service Framework

Framework para criação de workflows multi-etapas com LangGraph, permitindo que agentes conversacionais executem processos complexos que requerem múltiplas interações com o usuário.

## 📋 Índice

- [Visão Geral](#visão-geral)
- [Conceitos Principais](#conceitos-principais)
- [Arquitetura](#arquitetura)
- [Como Criar um Workflow](#como-criar-um-workflow)
- [Navegação Não-Linear](#navegação-não-linear)
- [Gerenciamento de Estado](#gerenciamento-de-estado)
- [Testes](#testes)
- [Exemplos](#exemplos)

---

## Visão Geral

O Multi-Step Service permite criar fluxos de conversação complexos onde:
- **Agente coleta dados do usuário passo a passo**
- **Estado é persistido entre interações** (JSON ou Redis)
- **Validação automática** via Pydantic schemas
- **Navegação não-linear** - usuário pode voltar e mudar respostas anteriores
- **Integração com APIs externas** de forma assíncrona

### Caso de Uso: IPTU Workflow

```
1. Usuário: "Quero pagar meu IPTU"
   → Sistema pede: inscrição imobiliária

2. Usuário: "01234567890123"
   → Sistema pede: ano de exercício

3. Usuário: "2025"
   → Sistema consulta API e mostra guias disponíveis
   → Sistema pede: qual guia deseja pagar

4. Usuário: "00"
   → Sistema consulta cotas da guia
   → Sistema pede: quais cotas deseja pagar

5. Usuário: "1, 2, 3"
   → Sistema pede: confirmação dos dados

6. Usuário: "Sim"
   → Sistema gera DARMs e exibe boletos
```

---

## Conceitos Principais

### 1. **ServiceState**

Estado compartilhado entre todos os nós do workflow:

```python
class ServiceState(BaseModel):
    user_id: str              # Identificação do usuário
    service_name: str         # Nome do serviço (ex: "iptu_pagamento")
    status: str               # "progress" | "completed" | "error"
    data: Dict[str, Any]      # Dados persistidos entre interações
    internal: Dict[str, Any]  # Flags internas (não persistidas)
    payload: Dict[str, Any]   # Dados temporários da requisição atual
    agent_response: Optional[AgentResponse]  # Resposta para o agente
```

**Campos importantes:**
- **`data`**: Dados coletados do usuário (persistidos)
- **`internal`**: Flags de controle (ex: `has_consulted_guias`)
- **`payload`**: Entrada do usuário na interação atual
- **`agent_response`**: O que será retornado ao agente

### 2. **BaseWorkflow**

Classe base para todos os workflows. Gerencia execução do grafo LangGraph.

```python
class MeuWorkflow(BaseWorkflow):
    service_name = "meu_servico"
    description = "Descrição do serviço"

    def build_graph(self) -> StateGraph[ServiceState]:
        """Constrói o grafo de execução"""
        graph = StateGraph(ServiceState)

        # Adiciona nós
        graph.add_node("coletar_dados", self._coletar_dados)
        graph.add_node("processar", self._processar)

        # Define fluxo
        graph.set_entry_point("coletar_dados")
        graph.add_edge("coletar_dados", "processar")
        graph.add_edge("processar", END)

        return graph
```

### 3. **AgentResponse**

Resposta estruturada retornada ao agente após cada execução:

```python
class AgentResponse(BaseModel):
    service_name: Optional[str] = None
    description: str  # Mensagem para o usuário
    payload_schema: Optional[Dict] = None  # Schema do próximo campo esperado
    error_message: Optional[str] = None
    data: Dict[str, Any] = {}  # Dados atuais do workflow
```

**Como funciona:**
- **`description`**: Texto exibido ao usuário
- **`payload_schema`**: Se presente, agente sabe que precisa coletar mais dados
- **`payload_schema = None`**: Workflow finalizado ou aguardando qualquer input

---

## Arquitetura

```
┌─────────────────────────────────────────────────────────────┐
│                         USUÁRIO                              │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                    MULTI_STEP_SERVICE                        │
│  (Ferramenta MCP que recebe payload do usuário)             │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                      ORCHESTRATOR                            │
│  - Carrega/Salva estado (StateManager)                      │
│  - Instancia workflow correto                               │
│  - Executa workflow.execute(state, payload)                 │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                   WORKFLOW (LangGraph)                       │
│  ┌─────────────┐      ┌──────────────┐      ┌────────────┐ │
│  │   Nó 1      │─────→│    Nó 2      │─────→│   Nó 3     │ │
│  │ Coleta CPF  │      │ Valida CPF   │      │ Processa   │ │
│  └─────────────┘      └──────────────┘      └────────────┘ │
│                                                              │
│  - Cada nó recebe ServiceState                              │
│  - Cada nó pode definir agent_response                      │
│  - Grafo executa até pausar (END) ou erro                   │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                     STATE MANAGER                            │
│  - Persiste estado em JSON ou Redis                         │
│  - Chave: {user_id}_{service_name}                          │
└─────────────────────────────────────────────────────────────┘
```

---

## Como Criar um Workflow

### Passo 1: Definir Modelos Pydantic

Crie modelos para validar cada entrada do usuário:

```python
# core/models.py
from pydantic import BaseModel, Field

class ColetaCPFPayload(BaseModel):
    """Payload para coleta de CPF"""
    cpf: str = Field(..., description="CPF do usuário")

    @field_validator("cpf")
    @classmethod
    def valida_cpf(cls, v: str) -> str:
        # Remove formatação
        cpf_clean = re.sub(r'[^0-9]', '', v)

        if len(cpf_clean) != 11:
            raise ValueError("CPF deve ter 11 dígitos")

        return cpf_clean
```

### Passo 2: Criar Templates de Mensagens

Centralize todas as mensagens do workflow:

```python
# templates.py
class MeuWorkflowTemplates:
    @staticmethod
    def solicitar_cpf() -> str:
        return "📋 Por favor, informe seu **CPF** para continuar."

    @staticmethod
    def cpf_invalido() -> str:
        return "❌ CPF inválido. Por favor, verifique e tente novamente."

    @staticmethod
    def confirmacao_dados(cpf: str, nome: str) -> str:
        return f"""✅ **Confirmação de Dados**

**CPF:** {cpf}
**Nome:** {nome}

Os dados estão corretos?"""
```

### Passo 3: Implementar o Workflow

```python
# meu_workflow.py
from langgraph.graph import StateGraph, END
from src.tools.multi_step_service.core.base_workflow import BaseWorkflow, handle_errors
from src.tools.multi_step_service.core.models import ServiceState, AgentResponse

class MeuWorkflow(BaseWorkflow):
    service_name = "meu_servico"
    description = "Meu serviço personalizado"

    @handle_errors
    async def _coletar_cpf(self, state: ServiceState) -> ServiceState:
        """Coleta CPF do usuário"""

        # Se CPF veio no payload, valida e salva
        if "cpf" in state.payload:
            try:
                validated = ColetaCPFPayload.model_validate(state.payload)
                state.data["cpf"] = validated.cpf
                state.agent_response = None  # Continua para próximo nó
                return state
            except Exception as e:
                # Erro de validação - pede novamente
                state.agent_response = AgentResponse(
                    description=MeuWorkflowTemplates.cpf_invalido(),
                    payload_schema=ColetaCPFPayload.model_json_schema(),
                    error_message=str(e)
                )
                return state

        # Se já tem CPF salvo, continua
        if "cpf" in state.data:
            return state

        # Solicita CPF
        state.agent_response = AgentResponse(
            description=MeuWorkflowTemplates.solicitar_cpf(),
            payload_schema=ColetaCPFPayload.model_json_schema()
        )
        return state

    @handle_errors
    async def _processar(self, state: ServiceState) -> ServiceState:
        """Processa os dados coletados"""
        cpf = state.data.get("cpf")

        # Chama API externa
        resultado = await self.api_service.processar(cpf)

        # Retorna sucesso
        state.agent_response = AgentResponse(
            service_name=self.service_name,
            description=f"✅ Processado com sucesso! Resultado: {resultado}",
            payload_schema=None,  # Workflow finalizado
            data={"cpf": cpf, "resultado": resultado}
        )

        return state

    def build_graph(self) -> StateGraph[ServiceState]:
        """Constrói o grafo do workflow"""
        graph = StateGraph(ServiceState)

        # Adiciona nós
        graph.add_node("coletar_cpf", self._coletar_cpf)
        graph.add_node("processar", self._processar)

        # Define fluxo
        graph.set_entry_point("coletar_cpf")

        # Roteamento condicional
        graph.add_conditional_edges(
            "coletar_cpf",
            lambda state: END if state.agent_response else "processar",
            {"processar": "processar", END: END}
        )

        graph.add_edge("processar", END)

        return graph
```

### Passo 4: Registrar o Workflow

```python
# workflows/__init__.py
from src.tools.multi_step_service.workflows.meu_workflow.meu_workflow import MeuWorkflow

workflows = [
    MeuWorkflow,
    # ... outros workflows
]
```

---

## Navegação Não-Linear

⚡ **Novidade**: Permite que usuários "voltem" para steps anteriores e mudem suas respostas.

### Como Habilitar

No seu workflow, defina 3 atributos:

```python
class IPTUWorkflow(BaseWorkflow):
    service_name = "iptu_pagamento"

    # 1. Habilita navegação não-linear
    automatic_resets = True

    # 2. Define ordem dos steps principais
    step_order = [
        'inscricao_imobiliaria',
        'ano_exercicio',
        'guia_escolhida',
        'cotas_escolhidas'
    ]

    # 3. Define o que cada campo invalida quando muda
    step_dependencies = {
        'inscricao_imobiliaria': [
            'endereco', 'proprietario', 'ano_exercicio',
            'dados_guias', 'guia_escolhida', 'dados_cotas', 'cotas_escolhidas'
        ],
        'ano_exercicio': [
            'dados_guias', 'guia_escolhida', 'dados_cotas', 'cotas_escolhidas'
        ],
        'guia_escolhida': [
            'dados_cotas', 'cotas_escolhidas'
        ],
        'cotas_escolhidas': []  # Último step, não invalida nada
    }
```

### Como Funciona

**Cenário**: Usuário está no step 4 (escolha de cotas) mas envia `ano_exercicio: 2024`

1. **BaseWorkflow.execute()** detecta `automatic_resets=True`
2. **StepNavigator** detecta que `ano_exercicio` é step anterior (índice 1 < 3)
3. Remove campos dependentes: `dados_guias`, `guia_escolhida`, `dados_cotas`, `cotas_escolhidas`
4. Workflow continua normalmente a partir do novo ano

**Exemplo de interação:**

```
👤 Usuário: "Inscrição 12345678"
🤖 Sistema: "Qual ano?"

👤 Usuário: "2025"
🤖 Sistema: "Guias disponíveis: 00, 01. Qual deseja?"

👤 Usuário: "00"
🤖 Sistema: "Selecione as cotas: 1, 2, 3..."

👤 Usuário: "Na verdade, quero o ano 2024"  ← Volta para step anterior!
🤖 Sistema: [Reseta dados_guias, guia, cotas]
           "Guias disponíveis para 2024: 00, 01. Qual deseja?"
```

### Benefícios

✅ Usuário pode corrigir erros sem reiniciar
✅ Experiência mais natural e flexível
✅ Nenhuma modificação nos nós existentes
✅ Opt-in por workflow (não afeta workflows antigos)

---

## Gerenciamento de Estado

### StateManager

Gerencia persistência do estado do usuário:

```python
from src.tools.multi_step_service.core.state import StateManager, StateMode

# JSON (padrão)
state_manager = StateManager(
    user_id="user123",
    backend_mode=StateMode.JSON,
    data_dir="data"
)

# Redis
state_manager = StateManager(
    user_id="user123",
    backend_mode=StateMode.REDIS,
    redis_url="redis://localhost:6379"
)

# Ambos (JSON + Redis)
state_manager = StateManager(
    user_id="user123",
    backend_mode=StateMode.BOTH
)
```

### Estrutura de Dados

```json
{
  "user_id": "user123",
  "service_name": "iptu_pagamento",
  "status": "progress",
  "data": {
    "inscricao_imobiliaria": "01234567890123",
    "ano_exercicio": 2025,
    "guia_escolhida": "00"
  },
  "internal": {
    "has_consulted_guias": true,
    "failed_attempts_01234567890123": 1
  },
  "agent_response": {
    "description": "Selecione as cotas...",
    "payload_schema": { "cotas_escolhidas": "..." }
  }
}
```

**Persistência:**
- Arquivo: `data/{user_id}_{service_name}.json`
- Redis: Chave `{user_id}_{service_name}`

---

## Testes

### Estrutura de Testes

```
workflows/
  meu_workflow/
    tests/
      test_meu_workflow.py     # Testes de integração
      test_api_service.py      # Testes da API
      test_helpers.py          # Testes de utilitários
```

### Exemplo de Teste

```python
import pytest
from src.tools.multi_step_service.tool import multi_step_service

class TestMeuWorkflow:
    def setup_method(self):
        self.user_id = "test_user_123"
        self.service_name = "meu_servico"

    @pytest.mark.asyncio
    async def test_fluxo_completo(self):
        """Testa fluxo completo do início ao fim"""

        # STEP 1: Solicita CPF
        response1 = await multi_step_service.ainvoke({
            "service_name": self.service_name,
            "user_id": self.user_id,
            "payload": {"cpf": "12345678901"}
        })

        assert response1["error_message"] is None
        assert "processado com sucesso" in response1["description"].lower()
```

### Mock de APIs

Use API fake para testes:

```python
class MeuWorkflow(BaseWorkflow):
    def __init__(self, use_fake_api: bool = False):
        super().__init__()

        if use_fake_api or os.getenv("USE_FAKE_API") == "true":
            self.api_service = APIServiceFake()
        else:
            self.api_service = APIService()
```

---

## Exemplos

### Workflow IPTU (Completo)

Consulta e emissão de guias de IPTU da Prefeitura do Rio:

```
📁 workflows/iptu_pagamento/
  ├── iptu_workflow.py          # Workflow principal
  ├── core/
  │   ├── models.py              # Modelos Pydantic
  │   └── constants.py           # Constantes
  ├── api/
  │   ├── api_service.py         # Integração com API real
  │   └── api_service_fake.py    # Mock para testes
  ├── helpers/
  │   ├── utils.py               # Funções utilitárias
  │   └── state_helpers.py       # Helpers de estado
  ├── templates.py               # Mensagens do usuário
  └── tests/
      └── test_iptu_workflow.py  # 35 testes de integração
```

**Funcionalidades:**
- ✅ 9 nós no grafo
- ✅ Integração com API externa da Prefeitura
- ✅ Validação de inscrições
- ✅ Consulta de dívida ativa
- ✅ Geração de DARMs (boletos)
- ✅ Navegação não-linear
- ✅ 35 testes (94.3% de taxa de sucesso)

### Estrutura Mínima

Para criar um novo workflow simples:

```
workflows/
  meu_servico/
    __init__.py
    meu_workflow.py       # Classe principal
    templates.py          # Mensagens
    tests/
      test_meu_workflow.py
```

---

## Padrões e Boas Práticas

### 1. Nomeação de Nós

Use verbos que descrevem a ação:
- ✅ `_coletar_cpf`, `_validar_dados`, `_gerar_boleto`
- ❌ `_cpf`, `_dados`, `_boleto`

### 2. Roteamento Condicional

Use funções auxiliares para clareza:

```python
def _decide_after_validation(self, state: ServiceState):
    """Decide próximo passo após validação"""
    if state.agent_response is not None:
        return END  # Parou para pedir mais dados
    return "processar"  # Continua
```

### 3. Validação de Payload

Sempre use Pydantic para validação:

```python
if "campo" in state.payload:
    try:
        validated = MeuPayload.model_validate(state.payload)
        state.data["campo"] = validated.campo
        state.agent_response = None
    except Exception as e:
        state.agent_response = AgentResponse(
            description="Erro: campo inválido",
            payload_schema=MeuPayload.model_json_schema(),
            error_message=str(e)
        )
```

### 4. Tratamento de Erros

Use `@handle_errors` decorator:

```python
@handle_errors
async def _meu_no(self, state: ServiceState) -> ServiceState:
    # Se erro ocorrer, decorator captura e retorna AgentResponse com erro
    resultado = await self.api_service.call()
    return state
```

### 5. Logs

Use loguru para debug:

```python
from loguru import logger

logger.info(f"✅ Dados salvos: {state.data}")
logger.debug(f"🔍 Consultando API para inscrição: {inscricao}")
logger.warning(f"⚠️ API indisponível, tentando novamente")
logger.error(f"❌ Erro crítico: {str(e)}")
```

---

## Referências

- **LangGraph**: https://github.com/langchain-ai/langgraph
- **Pydantic**: https://docs.pydantic.dev/
- **Loguru**: https://github.com/Delgan/loguru

---

## Contribuindo

Para adicionar um novo workflow:

1. Crie pasta em `workflows/nome_workflow/`
2. Implemente classe herdando de `BaseWorkflow`
3. Registre em `workflows/__init__.py`
4. Adicione testes em `tests/`
5. Documente no README

---

**Versão:** 1.0.0
**Última atualização:** Dezembro 2024




### Melhorias

 🎯 OPORTUNIDADES DE MELHORIA NO MSS (se não migrar)

  Se você quiser melhorar MSS em vez de migrar:

  Opção 1: Helper Functions (reduce boilerplate 50%)

  # Novo helper em core/node_helpers.py
  async def collect_input(
      state: ServiceState,
      field: str,
      schema: Type[BaseModel],
      message_fn: Callable,
      **message_kwargs
  ):
      """Helper para coletar input do usuário com padrão consistente."""
      # Verifica payload
      if field in state.payload:
          try:
              validated = schema.model_validate(state.payload)
              state.data[field] = getattr(validated, field)
              state.agent_response = None
              return state
          except Exception as e:
              state.agent_response = AgentResponse(
                  description=message_fn(**message_kwargs),
                  payload_schema=schema.model_json_schema(),
                  error_message=f"Inválido: {e}"
              )
              return state

      # Verifica se já tem
      if field in state.data:
          state.agent_response = None
          return state

      # Pede input
      state.agent_response = AgentResponse(
          description=message_fn(**message_kwargs),
          payload_schema=schema.model_json_schema()
      )
      return state

  Uso:
  # Antes: 35 linhas
  async def _escolher_ano_exercicio(self, state: ServiceState) -> ServiceState:
      inscricao = state.data.get("inscricao_imobiliaria", "N/A")
      # ... 30 linhas de boilerplate ...
      return state

  # Depois: 8 linhas
  async def _escolher_ano_exercicio(self, state: ServiceState) -> ServiceState:
      return await collect_input(
          state, "ano_exercicio", EscolhaAnoPayload,
          IPTUMessageTemplates.escolher_ano,
          inscricao=state.data.get("inscricao_imobiliaria"),
          endereco=state.data.get("endereco"),
          proprietario=state.data.get("proprietario")
      )

  Redução: 35 → 8 linhas (77% menos código)

  ---
  Opção 2: Decorator Pattern

  # Decorator que transforma método simples em node completo
  def input_node(field: str, schema: Type[BaseModel]):
      def decorator(func):
          @wraps(func)
          async def wrapper(self, state: ServiceState) -> ServiceState:
              # Lógica automática de verificação
              if field in state.data:
                  return state

              if field in state.payload:
                  validated = schema.model_validate(state.payload)
                  state.data[field] = getattr(validated, field)
                  state.agent_response = None
                  return state

              # Chama função original para gerar mensagem
              message = await func(self, state)
              state.agent_response = AgentResponse(
                  description=message,
                  payload_schema=schema.model_json_schema()
              )
              return state
          return wrapper
      return decorator

  Uso:
  @input_node("ano_exercicio", EscolhaAnoPayload)
  async def _escolher_ano_exercicio(self, state: ServiceState) -> str:
      # Apenas retorna a mensagem!
      return IPTUMessageTemplates.escolher_ano(
          inscricao=state.data.get("inscricao_imobiliaria"),
          endereco=state.data.get("endereco"),
          proprietario=state.data.get("proprietario")
      )