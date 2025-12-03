# Workflow IPTU - Prefeitura do Rio de Janeiro

Workflow completo para consulta e emissão de guias de IPTU da Prefeitura do Rio de Janeiro usando o framework Multi-Step Service.

## 📋 Índice

- [Visão Geral](#visão-geral)
- [Como Funciona](#como-funciona)
- [Estrutura do Código](#estrutura-do-código)
- [Fluxo Detalhado](#fluxo-detalhado)
- [Navegação Não-Linear](#navegação-não-linear)
- [API Service](#api-service)
- [Casos Especiais](#casos-especiais)
- [Testes](#testes)
- [Desenvolvimento](#desenvolvimento)
- [Troubleshooting](#troubleshooting)

---

## Visão Geral

Este workflow implementa o fluxo completo de consulta de IPTU seguindo o fluxograma oficial da Prefeitura do Rio de Janeiro. Permite ao usuário:

- ✅ Consultar guias de IPTU por inscrição imobiliária
- ✅ Escolher ano de exercício fiscal (2020-2025)
- ✅ Selecionar guias específicas para pagamento
- ✅ Escolher cotas a pagar (cota única ou parceladas)
- ✅ Gerar boletos (DARMs) únicos ou separados
- ✅ **Navegação não-linear** - voltar e mudar respostas anteriores
- ✅ Consulta de dívida ativa quando IPTU foi migrado
- ✅ Continuar operações (mais cotas, outras guias, outro imóvel)

### Estatísticas

- **Nós no grafo**: 9 nós principais
- **Taxa de sucesso em testes**: 94.3% (33/35 testes passando)
- **Cobertura de casos**: Happy paths, edge cases, erros de API, validações

---

## Como Funciona

### Exemplo de Interação Conversacional

```
👤 Usuário: "Quero pagar meu IPTU"
🤖 Sistema: "📋 Por favor, informe a inscrição imobiliária do imóvel."

👤 Usuário: "01234567890123"
🤖 Sistema: "✅ Inscrição válida!
           📍 Endereço: RUA EXEMPLO, 123
           👤 Proprietário: JOÃO DA SILVA

           Por favor, escolha o ano de exercício fiscal (2020-2025):"

👤 Usuário: "2025"
🤖 Sistema: "📊 Guias disponíveis para 2025:

           **Guia 00** - Cota Única
           Valor: R$ 1.500,00 | Vencimento: 10/03/2025

           **Guia 01** - Parcelado (10x)
           Valor total: R$ 1.650,00

           Qual guia deseja pagar?"

👤 Usuário: "00"
🤖 Sistema: "📋 Cotas disponíveis da guia 00:

           [1] Cota única - R$ 1.500,00 (Venc: 10/03/2025) ✅ Em aberto

           Quais cotas deseja pagar? (digite os números separados por vírgula)"

👤 Usuário: "1"
🤖 Sistema: "✅ **Confirmação de Dados**

           📍 Inscrição: 01234567890123
           🏠 Endereço: RUA EXEMPLO, 123
           👤 Proprietário: JOÃO DA SILVA
           📄 Guia: 00
           💰 Cotas: 1
           🧾 Boletos: 1 DARM único

           Os dados estão corretos?"

👤 Usuário: "Sim"
🤖 Sistema: "✅ **Boleto Gerado com Sucesso!**

           **DARM 1/1**
           💰 Valor: R$ 1.500,00
           📅 Vencimento: 10/03/2025
           📄 Linha digitável: 12345.67890 12345.678901 23456.789012 1 12340000150000

           📥 PDF: [Baixar DARM](http://...)

           ✅ Processo finalizado!"
```

### Exemplo com Navegação Não-Linear

```
👤 Usuário: "Quero pagar IPTU"
🤖 Sistema: "Informe a inscrição..."

👤 Usuário: "01234567890123"
🤖 Sistema: "Escolha o ano..."

👤 Usuário: "2025"
🤖 Sistema: "Guias disponíveis para 2025: 00, 01. Qual deseja?"

👤 Usuário: "00"
🤖 Sistema: "Selecione as cotas: 1, 2, 3..."

👤 Usuário: "Na verdade, quero o ano 2024"  ← Volta para step anterior!
🤖 Sistema: [Reseta dados de guias, cotas]
           "Guias disponíveis para 2024: 00, 01, 02. Qual deseja?"

👤 Usuário: "01"
🤖 Sistema: "Cotas da guia 01: ..."
```

---

## Estrutura do Código

```
iptu_pagamento/
├── README.md                          # Este arquivo
├── iptu_workflow.py                   # ⭐ Workflow principal (StateGraph)
│
├── core/
│   ├── models.py                      # Modelos Pydantic para validação
│   └── constants.py                   # Constantes e valores mágicos
│
├── api/
│   ├── api_service.py                 # Cliente da API real da Prefeitura
│   ├── api_service_fake.py            # Mock da API para testes
│   └── exceptions.py                  # Exceções personalizadas
│
├── helpers/
│   ├── utils.py                       # Utilidades gerais
│   └── state_helpers.py               # Helpers para manipulação de state
│
├── templates.py                       # Templates de mensagens ao usuário
│
└── tests/
    └── test_iptu_workflow.py          # 35 testes de integração
```

### Arquivos Principais

#### `iptu_workflow.py`

Contém a classe `IPTUWorkflow` que herda de `BaseWorkflow`:

```python
class IPTUWorkflow(BaseWorkflow):
    service_name = "iptu_pagamento"
    description = "Consulta e emissão de guias de IPTU - Prefeitura do Rio de Janeiro."

    # Navegação não-linear
    automatic_resets = True
    step_order = [
        'inscricao_imobiliaria',
        'ano_exercicio',
        'guia_escolhida',
        'cotas_escolhidas'
    ]
    step_dependencies = {
        'inscricao_imobiliaria': ['endereco', 'proprietario', 'ano_exercicio', ...],
        'ano_exercicio': ['dados_guias', 'guia_escolhida', 'dados_cotas', ...],
        'guia_escolhida': ['dados_cotas', 'cotas_escolhidas'],
        'cotas_escolhidas': []
    }

    def build_graph(self) -> StateGraph[ServiceState]:
        # Constrói grafo com 9 nós
        ...
```

**Responsabilidades:**
- Define os 9 nós do workflow
- Configura roteamento condicional
- Gerencia integração com API
- Implementa navegação não-linear

#### `core/models.py`

Modelos Pydantic para validação de payloads:

```python
class InscricaoImobiliariaPayload(BaseModel):
    """Valida e limpa inscrição imobiliária."""
    inscricao_imobiliaria: str = Field(..., description="Inscrição imobiliária")

    @field_validator("inscricao_imobiliaria")
    @classmethod
    def validate_inscricao(cls, v: str) -> str:
        # Remove formatação
        clean = re.sub(r'[^0-9]', '', v)
        if len(clean) < 8 or len(clean) > 15:
            raise ValueError("Inscrição deve ter entre 8 e 15 dígitos")
        return clean
```

**Modelos disponíveis:**
- `InscricaoImobiliariaPayload` - Valida inscrição (8-15 dígitos)
- `EscolhaAnoPayload` - Valida ano (2020-2025)
- `EscolhaGuiasIPTUPayload` - Valida escolha de guia
- `EscolhaCotasParceladasPayload` - Valida lista de cotas
- `EscolhaFormatoDarmPayload` - Valida escolha de formato (único/separado)
- `ConfirmacaoDadosPayload` - Valida confirmação (bool)

#### `api/api_service.py`

Cliente da API real da Prefeitura do Rio:

```python
class IPTUAPIService:
    async def consultar_guias(self, inscricao: str, exercicio: int) -> DadosGuias:
        """Consulta guias disponíveis via API real."""
        ...

    async def obter_cotas(self, inscricao: str, exercicio: int, numero_guia: str) -> DadosCotas:
        """Obtém cotas de uma guia específica."""
        ...

    async def consultar_darm(self, ...) -> DadosDarm:
        """Gera DARM para pagamento."""
        ...
```

**Endpoints utilizados:**
- `GET /iptu/guias` - Consulta guias disponíveis
- `GET /iptu/cotas` - Obtém cotas de uma guia
- `POST /iptu/darm` - Gera DARM para pagamento
- `GET /iptu/imovel` - Dados do imóvel (endereço, proprietário)
- `GET /divida-ativa/consulta` - Consulta dívida ativa

#### `templates.py`

Templates de mensagens ao usuário:

```python
class IPTUMessageTemplates:
    @staticmethod
    def solicitar_inscricao() -> str:
        return "📋 Por favor, informe a **inscrição imobiliária** do imóvel."

    @staticmethod
    def dados_imovel(inscricao: str, proprietario: str, endereco: str, ...) -> str:
        return f"""
        ✅ **Dados do Imóvel**

        📍 Inscrição: {inscricao}
        🏠 Endereço: {endereco}
        👤 Proprietário: {proprietario}
        ...
        """
```

---

## Fluxo Detalhado

### Arquitetura do Grafo

```
                    ┌─────────────────────────┐
                    │  informar_inscricao     │
                    │  (Coleta inscrição)     │
                    └───────────┬─────────────┘
                                │
                                ▼
                    ┌─────────────────────────┐
                    │   escolher_ano          │
                    │   (Coleta ano fiscal)   │
                    └───────────┬─────────────┘
                                │
                                ▼
                    ┌─────────────────────────┐
                    │  consultar_guias        │
                    │  (Chama API)            │
                    └───────────┬─────────────┘
                                │
                        ┌───────┴────────┐
                        │ Tem guias?     │
                        └───────┬────────┘
                        Sim ↓   │ Não → END
                                ▼
                    ┌─────────────────────────┐
                    │  usuario_escolhe_guias  │
                    │  (Escolhe 00, 01, ...)  │
                    └───────────┬─────────────┘
                                │
                                ▼
                    ┌─────────────────────────┐
                    │  consultar_cotas        │
                    │  (Chama API)            │
                    └───────────┬─────────────┘
                                │
                                ▼
                    ┌─────────────────────────┐
                    │  usuario_escolhe_cotas  │
                    │  (Escolhe 1, 2, 3, ...) │
                    └───────────┬─────────────┘
                                │
                                ▼
                    ┌─────────────────────────┐
                    │  perguntar_formato_darm │
                    │  (Único ou separado?)   │
                    └───────────┬─────────────┘
                                │
                                ▼
                    ┌─────────────────────────┐
                    │  confirmacao_dados      │
                    │  (Confirma tudo?)       │
                    └───────────┬─────────────┘
                                │
                        ┌───────┴────────┐
                        │ Confirmou?     │
                        └───────┬────────┘
                        Sim ↓   │ Não → Reset
                                ▼
                    ┌─────────────────────────┐
                    │  gerar_darm             │
                    │  (Gera boletos)         │
                    └───────────┬─────────────┘
                                │
                                ▼
                              END
```

### Nós do Workflow

#### 1. `_informar_inscricao_imobiliaria`

**Função**: Coleta e valida inscrição imobiliária

**Lógica**:
1. Se payload tem `inscricao_imobiliaria`:
   - Valida com `InscricaoImobiliariaPayload`
   - Se válida: salva em `state.data` e busca dados do imóvel
   - Se inválida: retorna erro e pede novamente
2. Se já tem em `state.data`: continua
3. Senão: solicita inscrição

**State modificado**:
- `state.data["inscricao_imobiliaria"]` - Inscrição limpa (só números)
- `state.data["endereco"]` - Endereço do imóvel (via API)
- `state.data["proprietario"]` - Nome do proprietário (via API)

**Roteamento**:
- `agent_response != None` → END (aguarda input)
- `agent_response == None` → escolher_ano

---

#### 2. `_escolher_ano_exercicio`

**Função**: Coleta ano de exercício fiscal

**Lógica**:
1. Se payload tem `ano_exercicio`:
   - Valida com `EscolhaAnoPayload` (2020-2025)
   - Se válido: salva em `state.data`
   - Se inválido: retorna erro
2. Se já tem em `state.data`: continua
3. Senão: solicita ano

**State modificado**:
- `state.data["ano_exercicio"]` - Ano (int)

**Roteamento**:
- `agent_response != None` → END
- `agent_response == None` → consultar_guias

---

#### 3. `_consultar_guias_disponiveis`

**Função**: Consulta guias via API da Prefeitura

**Lógica**:
1. Verifica se já consultou (`STATE_HAS_CONSULTED_GUIAS`)
2. Chama `api_service.consultar_guias(inscricao, exercicio)`
3. **Se encontrou guias**:
   - Salva em `state.data["dados_guias"]`
   - Marca flag `STATE_HAS_CONSULTED_GUIAS = True`
   - Continua para seleção
4. **Se não encontrou guias**:
   - Tenta consultar dívida ativa (IPTU pode ter sido migrado)
   - Se tem dívida ativa: informa e pede novo ano
   - Se não tem: conta tentativa e pede novo ano
   - Se MAX_TENTATIVAS (3): pede nova inscrição

**State modificado**:
- `state.data["dados_guias"]` - Objeto com guias disponíveis
- `state.internal[STATE_HAS_CONSULTED_GUIAS]` - Flag de controle
- `state.internal[f"failed_attempts_{inscricao}"]` - Contador de tentativas

**Exceções tratadas**:
- `APIUnavailableError` - API fora do ar
- `AuthenticationError` - Problema de autenticação

**Roteamento**:
- Tem guias → usuario_escolhe_guias
- Erro com mensagem → END
- Sem ano → escolher_ano
- Sem inscrição → informar_inscricao

---

#### 4. `_usuario_escolhe_guias_iptu`

**Função**: Usuário escolhe qual guia pagar

**Lógica**:
1. Se payload tem `guia_escolhida`:
   - Valida com `EscolhaGuiasIPTUPayload`
   - Verifica se guia existe nos dados
   - Salva escolha
2. Se já tem em `state.data`: continua
3. Senão: formata e exibe guias disponíveis

**Formatação de guias**:
```
📊 Guias disponíveis para 2025:

**Guia 00** - Cota Única
Valor: R$ 1.500,00 | Vencimento: 10/03/2025

**Guia 01** - Parcelado (10 cotas)
Valor total: R$ 1.650,00
```

**State modificado**:
- `state.data["guia_escolhida"]` - Número da guia (ex: "00")

**Roteamento**:
- `agent_response != None` → END
- `agent_response == None` → consultar_cotas

---

#### 5. `_consultar_cotas`

**Função**: Consulta cotas da guia via API

**Lógica**:
1. Se já tem `dados_cotas`: pula
2. Chama `api_service.obter_cotas(inscricao, exercicio, guia)`
3. **Se encontrou cotas**:
   - Salva em `state.data["dados_cotas"]`
   - Filtra cotas pagas
   - Se todas pagas: informa e volta para seleção de guias
4. **Se não encontrou**: volta para seleção de guias

**State modificado**:
- `state.data["dados_cotas"]` - Objeto com cotas disponíveis

**Roteamento**:
- Erro → END
- Sem cotas → usuario_escolhe_guias
- Tem cotas → usuario_escolhe_cotas

---

#### 6. `_usuario_escolhe_cotas_iptu`

**Função**: Usuário escolhe quais cotas pagar

**Lógica**:
1. Se payload tem `cotas_escolhidas`:
   - Valida com `EscolhaCotasParceladasPayload`
   - Verifica se cotas existem e estão em aberto
   - Salva escolha
2. Se já tem: continua
3. Senão: formata e exibe cotas

**Validação especial**:
- Verifica se cota está paga (`esta_paga == True`)
- Se usuário tentar pagar cota já paga: retorna erro

**Formatação de cotas**:
```
📋 Cotas disponíveis da guia 00:

[1] Cota única - R$ 1.500,00 (Venc: 10/03/2025) ✅ Em aberto

Total: R$ 1.500,00
```

**State modificado**:
- `state.data["cotas_escolhidas"]` - Lista de números (ex: ["1", "2", "3"])

**Roteamento**:
- `agent_response != None` → END
- `agent_response == None` → perguntar_formato_darm

---

#### 7. `_perguntar_formato_darm`

**Função**: Pergunta se quer boleto único ou separado

**Lógica**:
1. Se só 1 cota: define automático como único
2. Se múltiplas cotas:
   - Pergunta se quer DARM separado ou único
   - Valida com `EscolhaFormatoDarmPayload`
3. Salva em `state.internal[STATE_USE_SEPARATE_DARM]`

**Opções**:
- **DARM único**: 1 boleto com valor total de todas as cotas
- **DARMs separados**: 1 boleto para cada cota

**State modificado**:
- `state.internal[STATE_USE_SEPARATE_DARM]` - Boolean

**Roteamento**:
- `agent_response != None` → END
- `agent_response == None` → confirmacao_dados

---

#### 8. `_confirmacao_dados_pagamento`

**Função**: Mostra resumo e pede confirmação

**Lógica**:
1. Verifica campos obrigatórios
2. Formata resumo dos dados:
   - Inscrição, endereço, proprietário
   - Guia escolhida
   - Cotas escolhidas
   - Número de boletos
3. Se payload tem `confirmacao`:
   - Se `True`: marca flag e continua
   - Se `False`: reseta tudo (mantém inscrição) e recomeça
4. Senão: exibe resumo e aguarda

**Resumo exibido**:
```
✅ **Confirmação de Dados**

📍 Inscrição: 01234567890123
🏠 Endereço: RUA EXEMPLO, 123
👤 Proprietário: JOÃO DA SILVA
📄 Guia: 00
💰 Cotas: 1
🧾 Boletos: 1 DARM único

Os dados estão corretos?
```

**State modificado**:
- `state.internal[STATE_IS_DATA_CONFIRMED]` - Boolean

**Roteamento**:
- Confirmou → gerar_darm
- Não confirmou → reset e volta ao início
- Aguardando → END

---

#### 9. `_gerar_darm`

**Função**: Gera DARMs via API e finaliza

**Lógica**:
1. Separa cotas conforme escolha de formato
2. Para cada grupo de cotas:
   - Chama `api_service.consultar_darm(...)`
   - Tenta baixar PDF com `download_pdf_darm(...)`
   - Salva dados do boleto
3. Formata mensagem final com boletos
4. Reset completo do estado
5. Retorna sucesso (sem `payload_schema` → permite qualquer pergunta)

**Dados retornados por boleto**:
- Tipo: "darm"
- Número da guia
- Cotas incluídas
- Valor
- Vencimento
- Código de barras
- Linha digitável
- URL do PDF

**Mensagem final**:
```
✅ **Boleto Gerado com Sucesso!**

**DARM 1/1**
💰 Valor: R$ 1.500,00
📅 Vencimento: 10/03/2025
📄 Linha digitável: 12345.67890 12345.678901 23456.789012 1 12340000150000

📥 PDF: [Baixar DARM](http://...)

✅ Processo finalizado!
```

**State modificado**:
- Reset completo após sucesso
- `agent_response.data["guias_geradas"]` - Lista de boletos gerados

**Roteamento**:
- Sempre → END (reset automático permite nova consulta)

---

## Navegação Não-Linear

⚡ **Novidade**: Este workflow suporta navegação não-linear, permitindo que usuários voltem para steps anteriores e mudem suas respostas.

### Como Funciona

O workflow define 3 atributos que habilitam navegação não-linear:

```python
class IPTUWorkflow(BaseWorkflow):
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
        'cotas_escolhidas': []
    }
```

### Cenários de Navegação

#### Cenário 1: Voltar do Step 4 para Step 2

```
Estado atual: Usuário está em escolha_cotas (step 4)
Payload recebido: {ano_exercicio: 2024}

Detecção:
  → StepNavigator detecta que 'ano_exercicio' é step 2
  → Step atual é 4 (tem 'cotas_escolhidas')
  → 2 < 4 → É step anterior!

Reset automático:
  → Remove dependências de 'ano_exercicio':
     - dados_guias ❌
     - guia_escolhida ❌
     - dados_cotas ❌
     - cotas_escolhidas ❌
  → Mantém:
     - inscricao_imobiliaria ✅
     - endereco ✅
     - proprietario ✅

Resultado:
  → Workflow continua normalmente do nó escolher_ano
  → Consulta guias para ano 2024
  → Usuário escolhe nova guia
  → Etc.
```

#### Cenário 2: Voltar do Step 3 para Step 1

```
Estado atual: Usuário está em usuario_escolhe_guias (step 3)
Payload recebido: {inscricao_imobiliaria: "99999999999999"}

Detecção:
  → 'inscricao_imobiliaria' é step 1
  → Nova inscrição é diferente da atual
  → Reset completo!

Reset automático:
  → Remove TODOS os campos dependentes:
     - endereco ❌
     - proprietario ❌
     - ano_exercicio ❌
     - dados_guias ❌
     - guia_escolhida ❌
     - dados_cotas ❌
     - cotas_escolhidas ❌

Resultado:
  → Workflow recomeça do zero com nova inscrição
  → Busca dados do novo imóvel
  → Pede ano de exercício
  → Etc.
```

#### Cenário 3: Mudança no Mesmo Step (Não é navegação)

```
Estado atual: Usuário está em usuario_escolhe_cotas
Payload recebido: {cotas_escolhidas: ["1", "2", "3"]}

Detecção:
  → 'cotas_escolhidas' é step 4
  → Step atual também é 4
  → Não é step anterior → SEM RESET

Resultado:
  → Nó processa normalmente
  → Atualiza escolha de cotas
  → Continua fluxo
```

### Implementação Interna

O reset automático é implementado em `BaseWorkflow.execute()`:

```python
async def execute(self, state: ServiceState, payload: Dict[str, Any]) -> ServiceState:
    state.payload = payload or {}

    # Auto-reset para navegação não-linear
    if self.automatic_resets and self.step_order and self.step_dependencies:
        state = self._auto_reset_for_previous_steps(state)

    # Compila e executa grafo
    graph = self.build_graph()
    compiled_graph = graph.compile()
    final_state_result = await compiled_graph.ainvoke(state)
    ...

def _auto_reset_for_previous_steps(self, state: ServiceState) -> ServiceState:
    from src.tools.multi_step_service.core.step_navigator import StepNavigator

    navigator = StepNavigator(
        step_order=self.step_order,
        step_dependencies=self.step_dependencies
    )
    return navigator.auto_reset(state)
```

### Benefícios

✅ **Experiência de usuário natural**: Pode corrigir erros sem reiniciar
✅ **Sem modificação nos nós**: Reset é transparente para a lógica dos nós
✅ **Opt-in**: Workflows antigos não são afetados
✅ **Testado**: 5 testes de integração específicos para navegação não-linear

---

## API Service

### API Real vs. API Fake

O workflow suporta dois modos de operação:

#### Modo Real (Produção)

```python
workflow = IPTUWorkflow(use_fake_api=False)  # Padrão
```

- Usa `IPTUAPIService`
- Conecta com API real da Prefeitura do Rio
- Requer credenciais de autenticação
- Dados reais de imóveis, guias e cotas

#### Modo Fake (Testes)

```python
# Via parâmetro
workflow = IPTUWorkflow(use_fake_api=True)

# Via variável de ambiente
os.environ["IPTU_USE_FAKE_API"] = "true"
workflow = IPTUWorkflow()
```

- Usa `IPTUAPIServiceFake`
- Dados mockados e previsíveis
- Sem chamadas HTTP reais
- Ideal para testes e desenvolvimento

### Endpoints da API Real

#### 1. Consultar Guias

```http
GET /iptu/guias?inscricao={inscricao}&exercicio={ano}
```

**Response**:
```json
{
  "inscricao_imobiliaria": "01234567890123",
  "exercicio": 2025,
  "guias": [
    {
      "numero_guia": "00",
      "tipo_guia": "Cota Única",
      "valor": 1500.00,
      "data_vencimento": "2025-03-10"
    },
    {
      "numero_guia": "01",
      "tipo_guia": "Parcelado",
      "valor": 1650.00,
      "data_vencimento": "2025-02-10",
      "quantidade_cotas": 10
    }
  ]
}
```

#### 2. Obter Cotas

```http
GET /iptu/cotas?inscricao={inscricao}&exercicio={ano}&guia={numero}
```

**Response**:
```json
{
  "inscricao_imobiliaria": "01234567890123",
  "exercicio": 2025,
  "numero_guia": "00",
  "cotas": [
    {
      "numero_cota": "1",
      "valor": 1500.00,
      "data_vencimento": "2025-03-10",
      "esta_paga": false
    }
  ]
}
```

#### 3. Gerar DARM

```http
POST /iptu/darm
Content-Type: application/json

{
  "inscricao_imobiliaria": "01234567890123",
  "exercicio": 2025,
  "numero_guia": "00",
  "cotas": ["1"]
}
```

**Response**:
```json
{
  "darm": {
    "codigo_barras": "12345678901234567890123456789012345678901234567890",
    "sequencia_numerica": "12345.67890 12345.678901 23456.789012 1 12340000150000",
    "valor": "1500.00",
    "valor_numerico": 1500.00,
    "data_vencimento": "2025-03-10"
  }
}
```

#### 4. Dados do Imóvel

```http
GET /iptu/imovel?inscricao={inscricao}
```

**Response**:
```json
{
  "inscricao_imobiliaria": "01234567890123",
  "endereco": "RUA EXEMPLO, 123 - CENTRO",
  "proprietario": "JOÃO DA SILVA"
}
```

#### 5. Consultar Dívida Ativa

```http
GET /divida-ativa/consulta?inscricao={inscricao}
```

**Response**:
```json
{
  "inscricao_imobiliaria": "01234567890123",
  "tem_divida_ativa": true,
  "cdas": [
    {
      "numero_cda": "2024/12345",
      "exercicio": "2024",
      "valor_original": 1500.00,
      "valor_atualizado": 1650.00
    }
  ],
  "efs": [],
  "parcelamentos": []
}
```

### Exceções da API

#### `APIUnavailableError`

**Quando ocorre**:
- API fora do ar
- Timeout de requisição
- Erro HTTP 5xx

**Tratamento no workflow**:
- Mantém estado atual
- Retorna mensagem amigável ao usuário
- Permite retry

**Exemplo**:
```python
try:
    dados_guias = await self.api_service.consultar_guias(inscricao, exercicio)
except APIUnavailableError as e:
    state.agent_response = AgentResponse(
        description=IPTUMessageTemplates.erro_api_indisponivel(str(e)),
        payload_schema=EscolhaAnoPayload.model_json_schema(),
        error_message=str(e)
    )
    return state
```

#### `AuthenticationError`

**Quando ocorre**:
- Credenciais inválidas
- Token expirado
- Erro HTTP 401/403

**Tratamento no workflow**:
- Retorna erro crítico
- Não permite retry (problema interno)

---

## Casos Especiais

### Caso 1: Dívida Ativa

**Cenário**: IPTU foi migrado para dívida ativa (não pago por muito tempo)

**Detecção**:
- Usuário informa inscrição e ano
- API retorna sem guias para aquele ano
- Workflow consulta dívida ativa automaticamente

**Fluxo**:
```
1. consultar_guias() retorna vazio
2. Workflow chama get_divida_ativa_info()
3. Se encontrou dívida ativa:
   → Informa ao usuário (CDA, EF, parcelamentos)
   → Pede novo ano (pode ter guias em outros anos)
4. Se não encontrou:
   → Conta tentativa
   → Se >= MAX_TENTATIVAS (3): pede nova inscrição
```

**Mensagem ao usuário**:
```
⚠️ **IPTU de 2024 Migrado para Dívida Ativa**

O IPTU do exercício de 2024 para a inscrição 01234567890123 foi
migrado para dívida ativa.

📋 **Débitos Encontrados:**

**CDA 2024/12345**
Exercício: 2024
Valor original: R$ 1.500,00
Valor atualizado: R$ 1.650,00

Para regularizar esta dívida, procure a Secretaria de Fazenda.

Deseja consultar outro ano de exercício?
```

### Caso 2: Cotas Já Pagas

**Cenário**: Usuário tenta pagar cota que já foi quitada

**Detecção**:
- Ao escolher cotas, workflow verifica `esta_paga` de cada cota
- Se usuário selecionar cota paga: retorna erro

**Fluxo**:
```
1. usuario_escolhe_cotas recebe {cotas_escolhidas: ["1", "2"]}
2. Verifica dados_cotas.cotas[0].esta_paga → True
3. Retorna erro informando que cota 1 já está paga
4. Aguarda nova seleção
```

**Validação especial**: `iptu_workflow.py:566-578`

**Mensagem ao usuário**:
```
❌ **Cotas Já Pagas**

As seguintes cotas selecionadas já foram quitadas e não podem
ser pagas novamente:

- Cota 1

Por favor, selecione apenas cotas em aberto.
```

### Caso 3: Todas as Cotas Quitadas

**Cenário**: Guia escolhida tem todas as cotas pagas

**Detecção**:
- Após consultar cotas, workflow filtra cotas em aberto
- Se lista vazia: todas quitadas

**Fluxo**:
```
1. consultar_cotas() obtém cotas da API
2. Filtra: cotas_em_aberto = [c for c in cotas if not c.esta_paga]
3. Se len(cotas_em_aberto) == 0:
   → Remove dados da guia escolhida
   → Volta para seleção de guias
```

**Mensagem ao usuário**:
```
✅ **Guia Totalmente Quitada**

A guia 00 já teve todas as suas cotas pagas.

Por favor, escolha outra guia ou informe outro imóvel.
```

### Caso 4: API Indisponível Durante Geração de DARM

**Cenário**: API falha ao gerar DARM após confirmação

**Tratamento**:
```
1. Usuario confirmou dados
2. gerar_darm() chama api_service.consultar_darm()
3. APIUnavailableError é levantada
4. Workflow:
   → Reseta para seleção de cotas (mantém guia escolhida)
   → Informa erro e permite retry
   → NÃO marca workflow como finalizado
```

**Código**: `iptu_workflow.py:790-799`

**Mensagem ao usuário**:
```
❌ **Erro ao Gerar Boleto**

Não foi possível gerar o DARM no momento devido a uma
indisponibilidade temporária da API.

Por favor, tente novamente em instantes.
```

### Caso 5: Inscrição Não Encontrada

**Cenário**: Inscrição não existe ou foi digitada errada

**Detecção**:
- Consulta guias retorna vazio
- Consulta dívida ativa retorna vazio
- Após MAX_TENTATIVAS_ANO (3) anos tentados

**Fluxo**:
```
1. Usuario informa inscricao
2. Escolhe ano 2025 → sem guias
3. Consulta divida ativa → sem divida
4. Incrementa failed_attempts_01234567890123 = 1
5. Escolhe ano 2024 → sem guias (tentativa 2)
6. Escolhe ano 2023 → sem guias (tentativa 3)
7. failed_attempts >= MAX_TENTATIVAS_ANO:
   → Reset completo
   → Pede nova inscrição
```

**Mensagem ao usuário**:
```
❌ **Inscrição Não Encontrada**

Não foram encontradas guias de IPTU para a inscrição 01234567890123
nos últimos anos consultados.

Por favor, verifique se a inscrição está correta e tente novamente.

Você pode consultar a inscrição do seu imóvel no carnê do IPTU ou
no site da Prefeitura.
```

---

## Testes

### Estrutura de Testes

```
tests/
└── test_iptu_workflow.py (35 testes)
    ├── TestIPTUWorkflowHappyPath (8 testes)
    │   ├── test_fluxo_completo_cota_unica
    │   ├── test_fluxo_completo_parcelado_darm_unico
    │   ├── test_fluxo_completo_parcelado_darm_separado
    │   └── ...
    │
    ├── TestIPTUWorkflowValidacoes (7 testes)
    │   ├── test_inscricao_invalida
    │   ├── test_ano_invalido
    │   ├── test_guia_invalida
    │   └── ...
    │
    ├── TestIPTUWorkflowErros (6 testes)
    │   ├── test_api_indisponivel
    │   ├── test_inscricao_nao_encontrada
    │   ├── test_guia_sem_cotas
    │   └── ...
    │
    ├── TestIPTUWorkflowContinuidade (6 testes)
    │   ├── test_pagar_mais_cotas_mesma_guia
    │   ├── test_pagar_outras_guias
    │   ├── test_pagar_outro_imovel
    │   └── ...
    │
    ├── TestIPTUWorkflowNonLinearNavigation (5 testes)
    │   ├── test_voltar_de_escolha_cotas_para_ano
    │   ├── test_voltar_de_selecao_cotas_para_guia
    │   ├── test_voltar_para_inscricao_reseta_tudo
    │   └── ...
    │
    └── TestIPTUWorkflowEdgeCases (3 testes)
        ├── test_todas_cotas_quitadas
        ├── test_selecionar_cota_paga
        └── ...
```

### Executar Testes

#### Todos os Testes

```bash
# Com pytest
pytest src/tools/multi_step_service/workflows/iptu_pagamento/tests/test_iptu_workflow.py -v

# Sem pytest (python asyncio)
python -m asyncio src/tools/multi_step_service/workflows/iptu_pagamento/tests/test_iptu_workflow.py
```

#### Classe Específica

```bash
pytest src/tools/multi_step_service/workflows/iptu_pagamento/tests/test_iptu_workflow.py::TestIPTUWorkflowHappyPath -v
```

#### Teste Específico

```bash
pytest src/tools/multi_step_service/workflows/iptu_pagamento/tests/test_iptu_workflow.py::TestIPTUWorkflowHappyPath::test_fluxo_completo_cota_unica -v
```

### Configuração de Testes

Todos os testes usam API Fake automaticamente:

```python
class TestIPTUWorkflowHappyPath:
    def setup_method(self):
        """Executado antes de cada teste."""
        self.user_id = f"test_user_{uuid.uuid4()}"
        self.service_name = "iptu_pagamento"
        self.inscricao_valida = "01234567890123"

        # Força uso de API fake
        os.environ["IPTU_USE_FAKE_API"] = "true"

    def teardown_method(self):
        """Executado depois de cada teste."""
        # Limpa estado persistido
        file_path = f"data/{self.user_id}_{self.service_name}.json"
        if os.path.exists(file_path):
            os.remove(file_path)
```

### Exemplo de Teste Completo

```python
# @pytest.mark.asyncio
async def test_fluxo_completo_cota_unica(self):
    """Testa fluxo completo: inscrição → ano → guia → cota → confirmação → DARM."""

    # STEP 1: Informar inscrição
    response1 = await multi_step_service.ainvoke({
        "service_name": self.service_name,
        "user_id": self.user_id,
        "payload": {"inscricao_imobiliaria": self.inscricao_valida}
    })

    assert response1["error_message"] is None
    assert "ano de exercício" in response1["description"].lower()
    assert response1["payload_schema"] is not None

    # STEP 2: Escolher ano
    response2 = await multi_step_service.ainvoke({
        "service_name": self.service_name,
        "user_id": self.user_id,
        "payload": {"ano_exercicio": 2025}
    })

    assert response2["error_message"] is None
    assert "guia" in response2["description"].lower()

    # STEP 3: Escolher guia
    response3 = await multi_step_service.ainvoke({
        "service_name": self.service_name,
        "user_id": self.user_id,
        "payload": {"guia_escolhida": "00"}
    })

    assert response3["error_message"] is None
    assert "cota" in response3["description"].lower()

    # STEP 4: Escolher cotas
    response4 = await multi_step_service.ainvoke({
        "service_name": self.service_name,
        "user_id": self.user_id,
        "payload": {"cotas_escolhidas": ["1"]}
    })

    assert response4["error_message"] is None
    assert "confirmação" in response4["description"].lower()

    # STEP 5: Confirmar
    response5 = await multi_step_service.ainvoke({
        "service_name": self.service_name,
        "user_id": self.user_id,
        "payload": {"confirmacao": True}
    })

    # Verifica DARM gerado
    assert response5["error_message"] is None
    assert "boleto gerado" in response5["description"].lower()
    assert response5["payload_schema"] is None  # Workflow finalizado
    assert "guias_geradas" in response5["data"]
    assert len(response5["data"]["guias_geradas"]) == 1
```

### Taxa de Sucesso

**Status atual**: 33/35 testes passando (94.3%)

**Testes falhando**:
- 1 teste com timeout (pode ser falso positivo)
- 1 teste com edge case raro

---

## Desenvolvimento

### Adicionar Nova Validação

**1. Adicione a constante em `core/constants.py`:**

```python
NOVO_LIMITE = 100
NOVA_MENSAGEM_ERRO = "Valor deve ser <= {limite}"
```

**2. Crie a função de validação em `core/models.py`:**

```python
class NovoPayload(BaseModel):
    campo: int = Field(..., description="Descrição do campo")

    @field_validator("campo")
    @classmethod
    def validate_campo(cls, v: int) -> int:
        if v > NOVO_LIMITE:
            raise ValueError(NOVA_MENSAGEM_ERRO.format(limite=NOVO_LIMITE))
        return v
```

**3. Use no nó do workflow:**

```python
@handle_errors
async def _meu_novo_no(self, state: ServiceState) -> ServiceState:
    if "campo" in state.payload:
        try:
            validated = NovoPayload.model_validate(state.payload)
            state.data["campo"] = validated.campo
            state.agent_response = None
            return state
        except Exception as e:
            state.agent_response = AgentResponse(
                description="Erro na validação",
                payload_schema=NovoPayload.model_json_schema(),
                error_message=str(e)
            )
            return state

    # Solicita campo
    state.agent_response = AgentResponse(
        description="Por favor, informe o campo.",
        payload_schema=NovoPayload.model_json_schema()
    )
    return state
```

### Adicionar Novo Nó ao Workflow

**1. Crie o método do nó em `iptu_workflow.py`:**

```python
@handle_errors
async def _meu_novo_no(self, state: ServiceState) -> ServiceState:
    """
    Descrição do que este nó faz.

    Args:
        state: Estado compartilhado do workflow

    Returns:
        State atualizado com agent_response definido ou None
    """
    # Lógica do nó

    # Se precisa parar e pedir input
    state.agent_response = AgentResponse(
        description="Mensagem ao usuário",
        payload_schema=MeuPayload.model_json_schema()
    )

    # Se pode continuar
    state.agent_response = None

    return state
```

**2. Adicione ao grafo em `build_graph()`:**

```python
def build_graph(self) -> StateGraph[ServiceState]:
    graph = StateGraph(ServiceState)

    # Adiciona todos os nós
    graph.add_node("meu_novo_no", self._meu_novo_no)

    # ...
```

**3. Conecte com edges:**

```python
# Edge simples
graph.add_edge("no_anterior", "meu_novo_no")
graph.add_edge("meu_novo_no", "proximo_no")

# Edge condicional
graph.add_conditional_edges(
    "meu_novo_no",
    self._roteador_condicional,
    {
        "opcao_a": "no_a",
        "opcao_b": "no_b",
        END: END
    }
)
```

**4. Crie roteador condicional (se necessário):**

```python
def _roteador_condicional(self, state: ServiceState) -> str:
    """Decide próximo nó baseado no estado."""
    if state.agent_response is not None:
        return END  # Parou para pedir input

    if state.data.get("alguma_condicao"):
        return "no_a"
    else:
        return "no_b"
```

### Adicionar Novo Template de Mensagem

Em `templates.py`:

```python
class IPTUMessageTemplates:
    @staticmethod
    def nova_mensagem(param1: str, param2: int) -> str:
        """
        Template para nova mensagem ao usuário.

        Args:
            param1: Descrição do parâmetro 1
            param2: Descrição do parâmetro 2

        Returns:
            Mensagem formatada em Markdown
        """
        return f"""
📋 **Título da Mensagem**

Param1: {param1}
Param2: {param2}

Por favor, escolha uma opção.
        """.strip()
```

### Adicionar Novo Helper

Em `helpers/utils.py`:

```python
def minha_funcao_helper(state: ServiceState, parametro: str) -> bool:
    """
    Descrição do que a função faz.

    Args:
        state: Estado do workflow
        parametro: Descrição do parâmetro

    Returns:
        Resultado da operação
    """
    # Lógica da função
    return resultado
```

---

## Troubleshooting

### Problema: "Inscrição inválida"

**Causa**: Inscrição tem menos de 8 ou mais de 15 dígitos

**Solução**:
- Verificar se inscrição está correta no carnê do IPTU
- Remover caracteres especiais (pontos, traços)
- Workflow aceita com ou sem formatação

**Exemplo válido**: `01234567890123` ou `0123.456.789-0123`

---

### Problema: "Nenhuma guia encontrada"

**Causas possíveis**:
1. IPTU foi migrado para dívida ativa
2. Ano escolhido não tem guias
3. Inscrição não existe

**Solução**:
1. Workflow consulta automaticamente dívida ativa
2. Se encontrou: informa débitos e pede novo ano
3. Se não encontrou: tenta MAX_TENTATIVAS (3) anos
4. Se ainda não encontrou: pede nova inscrição

---

### Problema: "API indisponível"

**Causa**: Servidor da Prefeitura fora do ar ou lento

**Solução**:
- Workflow mantém estado atual
- Retorna mensagem amigável
- Permite retry sem perder dados
- Não reseta dados já coletados

**Retry**: Usuário pode enviar mesma informação novamente

---

### Problema: "Erro ao gerar DARM"

**Causas possíveis**:
1. API indisponível
2. Cotas já foram pagas por outro meio
3. Problema de autenticação

**Solução**:
1. Workflow reseta para seleção de cotas
2. Mantém guia escolhida
3. Permite tentar com outras cotas

---

### Problema: Workflow "travou" em um step

**Causa**: Estado persistido pode estar corrompido

**Solução**:

```bash
# Limpar estado persistido do usuário
rm data/{user_id}_iptu_pagamento.json

# Ou via código
import os
file_path = f"data/{user_id}_iptu_pagamento.json"
if os.path.exists(file_path):
    os.remove(file_path)
```

---

### Problema: Testes falhando com "ModuleNotFoundError"

**Causa**: Dependências não instaladas

**Solução**:

```bash
pip install -r requirements.txt

# Ou manualmente
pip install pydantic loguru httpx
```

---

### Problema: "Cota já foi paga"

**Causa**: Usuário tentou pagar cota que já foi quitada

**Solução**:
- Workflow detecta automaticamente
- Retorna erro informativo
- Solicita nova seleção de cotas
- Mostra quais cotas estão disponíveis

---

## Constantes Importantes

### Validação

```python
# core/constants.py

# Anos
ANO_MIN_VALIDO = 2020          # Ano mínimo aceito
ANO_MAX_VALIDO = 2025          # Ano máximo aceito

# Inscrição
INSCRICAO_MIN_LENGTH = 8       # Tamanho mínimo (sem formatação)
INSCRICAO_MAX_LENGTH = 15      # Tamanho máximo

# Tentativas
MAX_TENTATIVAS_ANO = 3         # Máx tentativas antes de pedir nova inscrição
```

### Chaves de State

```python
# Flags de controle (state.internal)
STATE_IS_DATA_CONFIRMED = "is_data_confirmed"
STATE_HAS_CONSULTED_GUIAS = "has_consulted_guias"
STATE_USE_SEPARATE_DARM = "use_separate_darm"
STATE_FAILED_ATTEMPTS_PREFIX = "failed_attempts_"

# Dados persistidos (state.data)
# inscricao_imobiliaria, ano_exercicio, guia_escolhida, cotas_escolhidas
# endereco, proprietario, dados_guias, dados_cotas, divida_ativa_data
```

---

## Referências

- **Framework Multi-Step Service**: [README Principal](../../README.md)
- **LangGraph Documentation**: https://python.langchain.com/docs/langgraph
- **Pydantic Validation**: https://docs.pydantic.dev/latest/concepts/validators/
- **API Prefeitura do Rio**: https://api.dados.rio/ (documentação oficial)
- **Loguru**: https://github.com/Delgan/loguru

---

## Contribuindo

Ao contribuir para este workflow, siga estas diretrizes:

### 1. Convenções de Nomenclatura

- **Constantes**: `UPPER_SNAKE_CASE`
- **Funções/métodos**: `snake_case`
- **Classes**: `PascalCase`
- **State keys**: Prefixos padronizados
  - `is_*` para booleanos de estado
  - `has_*` para flags de ação completada
  - `wants_*` para intenções do usuário

### 2. Testes

- Adicione testes para qualquer novo código
- Mantenha taxa de sucesso >= 90%
- Use API fake em todos os testes
- Cleanup de estado em `teardown_method()`

### 3. Documentação

Use docstrings com:
```python
def funcao_exemplo(param1: str, param2: int) -> bool:
    """
    Breve descrição de uma linha.

    Descrição detalhada se necessário, explicando o comportamento,
    casos especiais, etc.

    Args:
        param1: Descrição do primeiro parâmetro
        param2: Descrição do segundo parâmetro

    Returns:
        Descrição do que retorna

    Raises:
        ValueError: Quando param1 está vazio

    Examples:
        >>> funcao_exemplo("teste", 42)
        True
    """
```

### 4. Type Hints

Use type hints em TODAS as funções:

```python
from typing import Dict, List, Optional

async def processar_dados(
    state: ServiceState,
    dados: Dict[str, Any],
    opcoes: Optional[List[str]] = None
) -> ServiceState:
    ...
```

### 5. Magic Values

Extraia para `constants.py`:

```python
# ❌ Ruim
if tentativas >= 3:
    ...

# ✅ Bom
if tentativas >= MAX_TENTATIVAS_ANO:
    ...
```

### 6. Logs

Use loguru com níveis apropriados:

```python
from loguru import logger

logger.debug(f"🔍 Detalhes para debug: {state.data}")
logger.info(f"✅ Operação bem-sucedida")
logger.warning(f"⚠️ Situação atípica mas tratada")
logger.error(f"❌ Erro que precisa atenção")
```

---

**Versão**: 2.0.0
**Última atualização**: Dezembro 2024
**Maintainer**: Equipe EMD
