# Sistema de Versionamento de Tools MCP

Este sistema resolve o problema de cache de tools no lado do agente, garantindo que mudanças nos prompts sejam detectadas automaticamente.

## 🎯 Problema Resolvido

Quando você altera os prompts/instruções de uma tool, o agente pode continuar usando a versão antiga em cache. Este sistema força o agente a recarregar quando há mudanças.

## 🔧 Como Funciona

1. **Versão na Descrição**: A tool mostra sua versão na descrição: `[TOOL_VERSION: v2d8ae04]`
2. **Versão na Resposta**: A resposta inclui metadados com a mesma versão
3. **Detecção de Mudança**: O agente compara versões e rechama quando diferem

## 📁 Arquivos do Sistema

- `src/utils/tool_version.json` - Armazena a versão atual
- `src/utils/tool_versioning.py` - Funções de versionamento  
- `.github/workflows/update-tool-version.yaml` - GitHub Action automática

## 🚀 Uso Automático (Recomendado)

A GitHub Action atualiza automaticamente a versão a cada push:

```yaml
# Triggers automáticos:
on:
  push:
    branches: [main, staging]
    paths: ['src/**', 'src/utils/tool_version.json']
```

## 🛠️ Uso Manual

### Ver versão atual:
```bash
python -m src.utils.tool_versioning --show
```

### Atualizar versão manualmente:
```bash
python -m src.utils.tool_versioning
```

## 📋 Exemplo de Saída

### Descrição da Tool:
```
[TOOL_VERSION: v2d8ae04] Obtém instruções e categorias disponíveis...
```

### Resposta da Tool:
```json
{
  "_tool_metadata": {
    "version": "v2d8ae04",
    "last_updated": "2025-09-03T13:21:33Z",
    "description": "Tool version for cache invalidation"
  },
  "data": {
    "instrucoes": [...],
    "categorias": [...]
  }
}
```

## 🤖 Instrução para o Agente

**Adicione esta instrução aos prompts do seu agente:**

> "Sempre compare o TOOL_VERSION na descrição das tools com a versão da sua última chamada. Se a versão mudou, rechame a tool imediatamente para obter as informações mais atualizadas."

## 🔄 Fluxo de Trabalho

1. **Desenvolver**: Altere os prompts/instruções
2. **Commit**: `git commit -m "update tool prompts"`  
3. **Push**: `git push origin main`
4. **Automático**: GitHub Action atualiza versão para o hash do commit
5. **Detecção**: Agente detecta nova versão e atualiza automaticamente

## ⚙️ Configuração Técnica

### Para adicionar versionamento a uma tool:

```python
from src.utils.tool_versioning import add_tool_version, get_tool_version_from_file

# Na descrição da tool
TOOL_VERSION = get_tool_version_from_file()["version"]

@mcp.tool()
async def minha_tool() -> dict:
    f"""
    [TOOL_VERSION: {TOOL_VERSION}] Descrição da tool...
    """
    response = {"dados": "..."}
    return add_tool_version(response)
```

## 🎯 Benefícios

- ✅ **Zero configuração**: Funciona automaticamente após push
- ✅ **Cache invalidation**: Agente sempre usa versão mais recente  
- ✅ **Baseado em git**: Versão muda apenas com commits reais
- ✅ **Transparente**: Agente vê versão antes de chamar
- ✅ **Robusto**: Fallbacks para casos de erro

## 🚨 Troubleshooting

### Versão não atualiza:
1. Verifique se a GitHub Action executou com sucesso
2. Execute manualmente: `python -m src.utils.tool_versioning`

### Agente não detecta mudanças:
1. Confirme que o agente recebeu a instrução de comparar versões
2. Verifique se a versão aparece na descrição da tool

### Erro "vERROR":
1. Problema ao ler git ou arquivo de versão
2. Execute manualmente para corrigir
