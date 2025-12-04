# Multi-Hook Services Framework - Roadmap para Produção

## 📋 Status Atual (POC v1.0.0)

### ✅ Implementado

#### Core Framework
- ✅ `BaseFlow` - Classe base com hooks (~496 linhas)
- ✅ `FlowExecutor` - Executor procedural (~184 linhas)
- ✅ `FlowExceptions` - Exceções de controle (~62 linhas)
- ✅ Navegação não-linear automática
- ✅ Cache de API automático
- ✅ Hooks: `use_input()`, `use_api()`, `use_choice()`, `use_multi_choice()`, `confirm()`
- ✅ Testes completos (3/3 passando)

#### Workflows de Exemplo
- ✅ IPTU Flow (~344 linhas vs 992 do LangGraph)
- ✅ Redução de código: 65%

### ❌ Faltando (Para Paridade com multi_step_service)

#### 1. State Management
**Problema:** Atualmente usa `ServiceState` do `multi_step_service`
```python
# ❌ Dependência externa
from src.tools.multi_step_service.core.models import ServiceState
```

**Falta:**
- ❌ State Manager próprio
- ❌ Backend JSON próprio (paridade com multi_step_service)
- ❌ Backend Redis próprio (opcional, para performance)

#### 2. Models
**Problema:** Usa `AgentResponse` do `multi_step_service`
```python
# ❌ Dependência externa
from src.tools.multi_step_service.core.models import AgentResponse
```

**Falta:**
- ❌ Models próprios (FlowState, FlowResponse)

#### 3. MCP Tool
**Problema:** Não tem tool própria
```python
# ❌ Não existe
# @tool
# async def multi_hook_service(...):
#     ...
```

**Falta:**
- ❌ Tool MCP para expor workflows
- ❌ Orchestrator para dispatch de workflows

#### 4. Configuração
**Falta:**
- ❌ Config (env vars, backends)

---

## 🎯 Objetivo: Paridade Funcional 100% com multi_step_service

### O Que multi_step_service FAZ (Replicar):

| Feature | multi_step_service | multi_hook_services | Status |
|---------|-------------------|---------------------|--------|
| **State persistence** | ✅ JSON/Redis | ❌ Depende de externo | Sprint 1 |
| **Models** | ✅ ServiceState, AgentResponse | ❌ Depende de externo | Sprint 1 |
| **MCP Tool** | ✅ multi_step_service() | ❌ Não existe | Sprint 2 |
| **Orchestrator** | ✅ Dispatch workflows | ❌ Não existe | Sprint 2 |
| **Config** | ✅ Env vars | ❌ Não existe | Sprint 2 |
| **Validação Pydantic** | ✅ | ✅ Já funciona | ✅ |
| **Cache API** | ✅ | ✅ Já funciona | ✅ |
| **Navegação não-linear** | ✅ Manual | ✅ Automática | ✅ |
| **Error handling** | ✅ | ✅ Já funciona | ✅ |

---

## 📝 Plano de Implementação (MVP)

### Sprint 1: State Management (CRÍTICO) - ~400 linhas, 2-3h

#### 1.1 - Criar Models Próprios

**Arquivo:** `src/tools/multi_hook_services/core/models.py`

```python
from typing import Dict, Any, Optional
from pydantic import BaseModel, Field
from datetime import datetime

class FlowState(BaseModel):
    """
    Estado de um flow, persistido entre execuções.

    100% equivalente ao ServiceState do multi_step_service.
    """
    # Identificação
    user_id: str
    flow_name: str

    # Estado da execução
    status: str = "progress"  # progress | completed | error | cancelled

    # Dados coletados (persistidos)
    data: Dict[str, Any] = Field(default_factory=dict)

    # Dados temporários (limpos a cada execução)
    payload: Dict[str, Any] = Field(default_factory=dict)

    # Dados internos (cache, flags, etc)
    internal: Dict[str, Any] = Field(default_factory=dict)

    # Metadados
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    # Response do agente (última resposta)
    agent_response: Optional["FlowResponse"] = None


class FlowResponse(BaseModel):
    """
    Resposta do flow para o agente.

    100% equivalente ao AgentResponse do multi_step_service.
    """
    # Identificação
    flow_name: str

    # Mensagem para o usuário
    description: str

    # Schema do próximo input esperado (None = workflow completo)
    payload_schema: Optional[Dict[str, Any]] = None

    # Dados retornados
    data: Dict[str, Any] = Field(default_factory=dict)

    # Erro (se houver)
    error_message: Optional[str] = None
```

**Estimativa:** ~100 linhas

---

#### 1.2 - Criar State Manager Interface

**Arquivo:** `src/tools/multi_hook_services/state/state_manager.py`

```python
from abc import ABC, abstractmethod
from typing import Optional
from src.tools.multi_hook_services.core.models import FlowState

class StateManager(ABC):
    """
    Interface abstrata para state management.

    Mesma interface do multi_step_service.
    """

    @abstractmethod
    async def load_state(self, user_id: str, flow_name: str) -> FlowState:
        """Carrega estado do usuário para o flow."""
        pass

    @abstractmethod
    async def save_state(self, state: FlowState) -> None:
        """Salva estado do usuário."""
        pass

    @abstractmethod
    async def delete_state(self, user_id: str, flow_name: str) -> None:
        """Remove estado do usuário."""
        pass

    @abstractmethod
    async def list_states(self, user_id: str) -> list[FlowState]:
        """Lista todos os estados do usuário."""
        pass
```

**Estimativa:** ~50 linhas

---

#### 1.3 - Criar JSON Backend

**Arquivo:** `src/tools/multi_hook_services/state/json_backend.py`

**Implementação:** Mesma lógica do multi_step_service/state/json_backend.py

```python
import json
from pathlib import Path
from loguru import logger

from src.tools.multi_hook_services.core.models import FlowState
from src.tools.multi_hook_services.state.state_manager import StateManager


class JsonBackend(StateManager):
    """
    Backend de persistência usando arquivos JSON.

    Estrutura:
    {base_dir}/
        ├── user1/
        │   ├── iptu_flow.json
        │   └── cor_flow.json
        └── user2/
            └── iptu_flow.json
    """

    def __init__(self, base_dir: str = "./.multi_hook_state"):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"JsonBackend initialized with base_dir={base_dir}")

    def _get_state_path(self, user_id: str, flow_name: str) -> Path:
        """Retorna path do arquivo de estado."""
        user_dir = self.base_dir / user_id
        user_dir.mkdir(parents=True, exist_ok=True)
        return user_dir / f"{flow_name}.json"

    async def load_state(self, user_id: str, flow_name: str) -> FlowState:
        """Carrega estado do JSON ou cria novo."""
        state_path = self._get_state_path(user_id, flow_name)

        if state_path.exists():
            try:
                with open(state_path, "r") as f:
                    data = json.load(f)
                    state = FlowState(**data)
                    logger.debug(f"State loaded for {user_id}/{flow_name}")
                    return state
            except Exception as e:
                logger.error(f"Error loading state: {e}")

        # Cria novo estado
        logger.info(f"Creating new state for {user_id}/{flow_name}")
        return FlowState(user_id=user_id, flow_name=flow_name)

    async def save_state(self, state: FlowState) -> None:
        """Salva estado no JSON."""
        state_path = self._get_state_path(state.user_id, state.flow_name)

        try:
            with open(state_path, "w") as f:
                json.dump(state.model_dump(), f, indent=2, default=str)
            logger.debug(f"State saved for {state.user_id}/{state.flow_name}")
        except Exception as e:
            logger.error(f"Error saving state: {e}")
            raise

    async def delete_state(self, user_id: str, flow_name: str) -> None:
        """Remove estado."""
        state_path = self._get_state_path(user_id, flow_name)

        if state_path.exists():
            state_path.unlink()
            logger.info(f"State deleted for {user_id}/{flow_name}")

    async def list_states(self, user_id: str) -> list[FlowState]:
        """Lista todos os estados do usuário."""
        user_dir = self.base_dir / user_id

        if not user_dir.exists():
            return []

        states = []
        for state_file in user_dir.glob("*.json"):
            try:
                with open(state_file, "r") as f:
                    data = json.load(f)
                    states.append(FlowState(**data))
            except Exception as e:
                logger.warning(f"Error loading state from {state_file}: {e}")

        return states
```

**Estimativa:** ~150 linhas

---

#### 1.4 - Testes Básicos

**Arquivo:** `src/tools/multi_hook_services/tests/test_state_manager.py`

Testes para JsonBackend:
- ✅ CRUD completo (create, read, update, delete)
- ✅ Listagem de estados
- ✅ Estado inexistente (cria novo)

**Estimativa:** ~100 linhas

---

### Sprint 2: Orchestrator e Tool (CRÍTICO) - ~500 linhas, 3-4h

#### 2.1 - Criar Config

**Arquivo:** `src/tools/multi_hook_services/core/config.py`

```python
"""Configurações do framework multi-hook-services."""

import os
from typing import Optional
from pydantic_settings import BaseSettings


class MultiHookConfig(BaseSettings):
    """
    Configurações do framework.

    Variáveis de ambiente:
    - MULTI_HOOK_STATE_BACKEND: json | redis (default: json)
    - MULTI_HOOK_JSON_STATE_DIR: Diretório para estado JSON (default: ./.multi_hook_state)
    - MULTI_HOOK_REDIS_HOST: Redis host (default: localhost)
    - MULTI_HOOK_REDIS_PORT: Redis port (default: 6379)
    - MULTI_HOOK_REDIS_PASSWORD: Redis password (opcional)
    """

    # Backend de estado
    STATE_BACKEND: str = "json"  # json | redis

    # JSON backend
    JSON_STATE_DIR: str = "./.multi_hook_state"

    # Redis backend (opcional)
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_PASSWORD: Optional[str] = None

    class Config:
        env_prefix = "MULTI_HOOK_"
        case_sensitive = False


# Singleton
_config: Optional[MultiHookConfig] = None

def get_config() -> MultiHookConfig:
    """Retorna configuração (singleton)."""
    global _config
    if _config is None:
        _config = MultiHookConfig()
    return _config
```

**Estimativa:** ~60 linhas

---

#### 2.2 - Criar Orchestrator

**Arquivo:** `src/tools/multi_hook_services/core/orchestrator.py`

```python
from typing import Dict, Type, Any, Optional
from loguru import logger

from src.tools.multi_hook_services.core.models import FlowState, FlowResponse
from src.tools.multi_hook_services.core.base_flow import BaseFlow
from src.tools.multi_hook_services.core.flow_executor import FlowExecutor
from src.tools.multi_hook_services.state.state_manager import StateManager


class FlowOrchestrator:
    """
    Orquestra execução de workflows.

    Equivalente ao Orchestrator do multi_step_service, mas para hooks.
    """

    def __init__(
        self,
        state_manager: StateManager,
        workflows: Dict[str, Type[BaseFlow]]
    ):
        """
        Args:
            state_manager: Backend de persistência
            workflows: Mapa {flow_name: FlowClass}
        """
        self.state_manager = state_manager
        self.workflows = workflows
        self.executor = FlowExecutor()

        logger.info(f"FlowOrchestrator initialized with {len(workflows)} workflows")

    def register_workflow(self, flow_name: str, flow_class: Type[BaseFlow]):
        """Registra um novo workflow."""
        self.workflows[flow_name] = flow_class
        logger.info(f"Workflow registered: {flow_name}")

    async def execute(
        self,
        flow_name: str,
        user_id: str,
        payload: Optional[Dict[str, Any]] = None
    ) -> FlowResponse:
        """
        Executa um workflow.

        Args:
            flow_name: Nome do workflow a executar
            user_id: ID do usuário
            payload: Dados enviados pelo usuário (opcional)

        Returns:
            FlowResponse com resultado da execução

        Raises:
            ValueError: Se workflow não existe
        """
        # Valida workflow
        if flow_name not in self.workflows:
            raise ValueError(
                f"Workflow '{flow_name}' not found. "
                f"Available: {list(self.workflows.keys())}"
            )

        logger.info(f"Executing workflow '{flow_name}' for user '{user_id}'")

        # 1. Carrega estado
        state = await self.state_manager.load_state(user_id, flow_name)

        # 2. Cria instância do flow
        flow_class = self.workflows[flow_name]
        flow = flow_class(state)

        # 3. Executa
        final_state = await self.executor.execute(flow, state, payload or {})

        # 4. Salva estado
        await self.state_manager.save_state(final_state)

        # 5. Retorna resposta
        logger.info(
            f"Workflow '{flow_name}' executed for user '{user_id}' "
            f"(status={final_state.status})"
        )

        return final_state.agent_response

    async def reset_workflow(self, flow_name: str, user_id: str) -> None:
        """Reseta estado de um workflow (volta para o início)."""
        await self.state_manager.delete_state(user_id, flow_name)
        logger.info(f"Workflow '{flow_name}' reset for user '{user_id}'")

    async def list_user_workflows(self, user_id: str) -> list[FlowState]:
        """Lista todos os workflows ativos do usuário."""
        return await self.state_manager.list_states(user_id)
```

**Estimativa:** ~150 linhas

---

#### 2.3 - Criar MCP Tool

**Arquivo:** `src/tools/multi_hook_services/tool.py`

**Implementação:** Mesma interface do multi_step_service/tool.py

```python
"""
MCP Tool para workflows baseados em hooks.

100% equivalente ao multi_step_service/tool.py mas usando hooks.
"""

from typing import Dict, Any, Optional
from langchain_core.tools import tool

from src.tools.multi_hook_services.core.orchestrator import FlowOrchestrator
from src.tools.multi_hook_services.state.json_backend import JsonBackend
from src.tools.multi_hook_services.core.config import get_config

# Importa workflows registrados
from src.tools.multi_hook_services.workflows.iptu_pagamento_hooks import IPTUFlow


DESCRIPTION = """
Sistema de workflows multi-step baseado em hooks com navegação não-linear automática.

Args:
    flow_name: Nome do workflow (ex: "iptu_pagamento")
    user_id: ID do usuário (usar 'agent' para agentes)
    payload: Dicionário com campos solicitados no payload_schema

COMO FUNCIONA:

1. FLUXO SEQUENCIAL (Normal):
   - Sistema fornece payload_schema indicando campo necessário
   - Envie APENAS o campo solicitado
   - Workflow avança automaticamente

2. NAVEGAÇÃO NÃO-LINEAR (Correção AUTOMÁTICA):
   - Se usuário quiser CORRIGIR dado anterior, envie esse campo
   - Sistema DETECTA automaticamente navegação não-linear
   - RESETA automaticamente steps posteriores
   - Continua do ponto corrigido

Exemplos:

[Normal]
Sistema: "Informe a inscrição" (payload_schema: {"inscricao_imobiliaria": ...})
Usuário: "12345678"
→ Envie: {"inscricao_imobiliaria": "12345678"}

[Correção]
Sistema no step 4: "Escolha cotas"
Usuário: "Quero mudar o ano para 2024" (step 2)
→ Envie: {"ano_exercicio": 2024}
→ Sistema AUTOMATICAMENTE reseta steps 3 e 4

WORKFLOWS DISPONÍVEIS:

__replace__available_workflows__

IMPORTANTE:
- Navegação não-linear é AUTOMÁTICA
- payload_schema=null indica workflow completo
"""


def _get_workflow_descriptions():
    """Gera descrição dinâmica dos workflows disponíveis."""
    workflows = {
        "iptu_pagamento": IPTUFlow,
        # Adicionar novos workflows aqui
    }

    if not workflows:
        return DESCRIPTION.replace("__replace__available_workflows__", "- Nenhum workflow disponível")

    descriptions = []
    for flow_name, flow_class in workflows.items():
        desc = getattr(flow_class, "description", "Sem descrição")
        descriptions.append(f"- {flow_name}: {desc}")

    description_text = "\n".join(descriptions)
    return DESCRIPTION.replace("__replace__available_workflows__", description_text)


# Orchestrator global (singleton)
_orchestrator = None

def _get_orchestrator() -> FlowOrchestrator:
    """Retorna orchestrator (singleton)."""
    global _orchestrator

    if _orchestrator is None:
        config = get_config()

        # Cria state manager (apenas JSON por enquanto)
        state_manager = JsonBackend(base_dir=config.JSON_STATE_DIR)

        # Registra workflows
        workflows = {
            "iptu_pagamento": IPTUFlow,
            # Adicionar novos workflows aqui
        }

        _orchestrator = FlowOrchestrator(state_manager, workflows)

    return _orchestrator


@tool(description=_get_workflow_descriptions())
async def multi_hook_service(
    flow_name: str,
    user_id: str,
    payload: Optional[Dict[str, Any]] = None
) -> dict:
    """
    Tool principal - executa workflows baseados em hooks.

    Interface 100% compatível com multi_step_service.
    """
    orchestrator = _get_orchestrator()

    # Executa workflow
    response = await orchestrator.execute(flow_name, user_id, payload or {})

    # Retorna resposta (mesmo formato que multi_step_service)
    return {
        "flow_name": response.flow_name,
        "description": response.description,
        "payload_schema": response.payload_schema,
        "data": response.data,
        "error_message": response.error_message
    }
```

**Estimativa:** ~200 linhas

---

#### 2.4 - Atualizar IPTU Flow

**Arquivo:** `src/tools/multi_hook_services/workflows/iptu_pagamento_hooks/iptu_flow.py`

**Modificações:**
```python
# ANTES (depende de multi_step_service)
from src.tools.multi_step_service.core.models import ServiceState, AgentResponse

# DEPOIS (100% auto-contido)
from src.tools.multi_hook_services.core.models import FlowState, FlowResponse

# Atualizar todos os tipos e referências
```

**Estimativa:** ~30 linhas de mudanças

---

#### 2.5 - Testes de Integração

**Arquivo:** `src/tools/multi_hook_services/tests/test_orchestrator.py`

Testes para:
- ✅ Execução completa de workflow
- ✅ Reset de workflow
- ✅ Workflow não existente (erro)
- ✅ Múltiplos usuários simultâneos

**Estimativa:** ~100 linhas

---

### Sprint 3: Redis Backend (OPCIONAL) - ~350 linhas, 3-4h

**APENAS se precisar performance/cache distribuído**

#### 3.1 - Redis Backend

**Arquivo:** `src/tools/multi_hook_services/state/redis_backend.py`

Implementação idêntica ao multi_step_service/state/redis_backend.py

**Estimativa:** ~200 linhas

#### 3.2 - Composite Backend (Redis + JSON)

**Arquivo:** `src/tools/multi_hook_services/state/composite_backend.py`

Estratégia:
- Redis = cache rápido
- JSON = persistência confiável

**Estimativa:** ~150 linhas

---

## 📊 Estimativa Total (MVP)

| Sprint | Componente | Linhas | Tempo |
|--------|-----------|--------|-------|
| **Sprint 1** | Models | ~100 | 30min |
| | StateManager Interface | ~50 | 15min |
| | JsonBackend | ~150 | 1h |
| | Testes State | ~100 | 45min |
| | **Subtotal Sprint 1** | **~400** | **2-3h** |
| **Sprint 2** | Config | ~60 | 20min |
| | Orchestrator | ~150 | 1h |
| | Tool MCP | ~200 | 1-1.5h |
| | Atualizar IPTU | ~30 | 30min |
| | Testes Orchestrator | ~100 | 45min |
| | **Subtotal Sprint 2** | **~540** | **3-4h** |
| **TOTAL MVP** | | **~940 linhas** | **5-7h** |

---

## ✅ Checklist de Paridade

### Estado Atual (POC)
- ✅ Hooks funcionais (use_input, use_api, etc)
- ✅ Navegação não-linear automática
- ✅ Cache de API automático
- ✅ Validação Pydantic
- ✅ Error handling básico

### Falta para Paridade (MVP)
- ❌ Models próprios (FlowState, FlowResponse)
- ❌ State Manager próprio
- ❌ Backend JSON próprio
- ❌ Orchestrator próprio
- ❌ Tool MCP próprio
- ❌ Config próprio

### Após MVP (100% auto-contido)
- ✅ State management próprio (JSON)
- ✅ Tool MCP funcional
- ✅ Orchestrator
- ✅ Configuração por env vars
- ✅ Testes básicos
- ✅ **PARIDADE FUNCIONAL COMPLETA**

---

## 🚀 Cronograma de Implementação

### Semana 1: Sprint 1 (State Management)
**Objetivo:** Framework 100% auto-contido para state

**Tarefas:**
1. Criar `core/models.py` (FlowState, FlowResponse)
2. Criar `state/state_manager.py` (interface)
3. Criar `state/json_backend.py`
4. Criar testes básicos
5. Atualizar BaseFlow/FlowExecutor para usar models próprios

**Resultado:** State management 100% próprio

---

### Semana 2: Sprint 2 (Orchestrator + Tool)
**Objetivo:** Framework funcional end-to-end

**Tarefas:**
1. Criar `core/config.py`
2. Criar `core/orchestrator.py`
3. Criar `tool.py` (MCP tool)
4. Atualizar IPTU flow para usar FlowState/FlowResponse
5. Criar testes de integração
6. Documentar uso

**Resultado:** Framework pronto para uso (MVP)

---

### Futuro: Sprint 3 (Redis - Opcional)
**Objetivo:** Performance com cache distribuído

**Tarefas:**
1. Criar `state/redis_backend.py`
2. Criar `state/composite_backend.py`
3. Testes Redis/Composite
4. Atualizar config para suportar Redis

**Resultado:** Framework otimizado para produção

---

## 📈 Comparação Final

| Aspecto | multi_step_service | multi_hook_services (MVP) |
|---------|-------------------|---------------------------|
| **State management** | ✅ JSON/Redis | ✅ JSON (Redis opcional) |
| **Models** | ✅ ServiceState, AgentResponse | ✅ FlowState, FlowResponse |
| **MCP Tool** | ✅ multi_step_service() | ✅ multi_hook_service() |
| **Orchestrator** | ✅ | ✅ |
| **Config** | ✅ Env vars | ✅ Env vars |
| **Navegação não-linear** | ✅ Manual | ✅ **Automática** |
| **Linhas de código** | ~9,155 | ~2,226 (POC + MVP) |
| **DX** | 3/10 | 9/10 |
| **Redução de código** | - | **~76%** |

---

## 🎯 Conclusão

### MVP = Paridade Funcional Completa

**Após Sprints 1 + 2:**
- ✅ 100% auto-contido (zero dependências externas)
- ✅ Mesma funcionalidade do multi_step_service
- ✅ State management próprio (JSON)
- ✅ Tool MCP próprio
- ✅ Orchestrator próprio
- ✅ **NAVEGAÇÃO NÃO-LINEAR AUTOMÁTICA** (vantagem!)
- ✅ **76% menos código** (vantagem!)
- ✅ **DX 3x melhor** (vantagem!)

**Pronto para:** Substituir multi_step_service em novos workflows

**Estimativa:** **~940 linhas, 5-7 horas de desenvolvimento**

---

## 📝 Próximo Passo

**Iniciar Sprint 1:**
1. Criar `src/tools/multi_hook_services/core/models.py`
2. Criar `src/tools/multi_hook_services/state/state_manager.py`
3. Criar `src/tools/multi_hook_services/state/json_backend.py`
4. Criar testes básicos

**Status:** 📝 Plano simplificado - Focado em PARIDADE, não em features extras

**Aguardando aprovação para começar.**
