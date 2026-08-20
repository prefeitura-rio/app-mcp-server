# Análise: Cache Redis do BigQuery (Equipamentos)

**Data:** 25 de agosto de 2026  
**Ramo:** `fix/CHATR-102-consultas-bigquery`  
**Contexto:** Medição do tamanho de cache no Redis após implementação de CHATR-115 e CHATR-125.

---

## Resumo Executivo

A implementação de cache em Redis para as queries de equipamentos **não representa risco de espaço**:

| Métrica | Valor | Status |
|---|---|---|
| **Tamanho máximo se cachear tudo** | 0,31 GB | ✅ Negligenciável |
| **Média por entrada de cache** | 16,2 KB | ✅ Pequena |
| **Número de células plus8 ** | 20.250 | ✅ Computável |
| **Potencial de otimização** | −54% (removendo campos não-essenciais) | 💡 Futuro |

**Conclusão:** Não há limite de espaço em Redis. A otimização é **opcional** e deve ser baseada em:
1. Se o agente realmente usa os campos `contato.redes_social`, `updated_at`, `plus10`, regiões, `esfera`
2. Se reduzir payload melhora tempo de resposta ou reduz I/O

---

## As 3 Queries

### Q1: Equipamentos por plus8 e coordenadas

**Função:** [get_pluscode_coords_equipments()](https://github.com/prefeitura-rio/app-mcp-server/blob/fix%2FCHARTR-102-consultas-bigquery/src/tools/equipments/pluscode_service.py#L51)

**Quando bate:** A cada chamada de `equipments_by_address`. O resultado é cacheado por `plus8 + categories` por 3600s.

**Antes (sem cache):** 100% das chamadas ia ao BigQuery.  
**Depois (com cache):** Só miss (primeira vez que plus8 é consultado) vai ao BigQuery.

**Tabelas consultadas:**
- `rj-iplanrio.plus_codes.codes` (891.000 linhas / 6,14 GB) — filtrada por `plus8`, escaneia ~0
- `rj-iplanrio.plus_codes.territorio` (2.767 linhas / 12 MB) — lida **inteira** a cada miss

**Custo (dry-run):** 11,42 MB por miss  
**Payload no cache:** 16,2 KB em média (p95: 16,8 KB)

**Frequência esperada:**
- **Alto acerto:** Regiões populares (`plus8` repetido) sofrem miss 1 vez a cada hora
- **Baixo acerto:** Zonas isoladas podem ter miss sempre (TTL curto para muitas chaves distintas)

---

### Q2: Categorias de equipamentos

**Função:** [get_category_equipments()](https://github.com/prefeitura-rio/app-mcp-server/blob/fix%2FCHARTR-102-consultas-bigquery/src/tools/equipments/pluscode_service.py#L228)

**Quando bate:** Cada vez que o agente precisa listar categorias disponíveis (instrução de ajuda).

**Cache:** Uma entrada de chave, reusada por todas as chamadas → **miss 1 vez a cada 3600s**.

**Custo (dry-run):** 21,6 MB por miss  
**Payload no cache:** ~26 pares (secretaria, categoria) → ~500 B  
**Resultado:** Praticamente grátis no Redis.

---

### Q3: Instruções por tema

**Função:** [get_tematic_instructions_for_equipments()](https://github.com/prefeitura-rio/app-mcp-server/blob/fix%2FCHARTR-102-consultas-bigquery/src/tools/equipments/pluscode_service.py#L275)

**Quando bate:** A cada chamada com tema específico (ex: "saude", "geral").

**Cache:** Por `tema`. Se agente usa poucos temas, poucos miss.

**Tabela:** `equipamentos_instrucoes` (tabela externa, Google Sheet)  
**Custo:** Varia com disponibilidade do Drive (falha esperada → degradação graciosa)

---

## Dados Brutos: Tamanho do Cache

Medição do namespace `equipments` (Q1 — o volume):

```
celulas_plus8:            20.250      (células grid distintas)
linhas_totais:            434.346     (linhas após dedup)
linhas_media_por_celula:  21,4        (em média 21-22 linhas por cell)
linhas_max:               22          (máximo 22)

bytes_medio_por_chave:    16.174      (tamanho médio de 1 entrada)
bytes_max_por_chave:      17.005      (pior caso)
bytes_p50:                16.092      (mediana)
bytes_p95:                16.771      (95º percentil)

gb_se_cachear_tudo:       0,31        (total em GB)
```

**Interpretação:**
- 20.250 chaves × 16,2 KB = 0,31 GB
- Redis típico: 32-256 GB → essa carga é 0,1% a 1% da capacidade
- Nem é necessário provisionar Redis exclusiva; compartilhado é suficiente

---

## Breakdown de Campos

**Top 10 campos por peso (em % do total):**

| Campo | Bytes/chave | % | Remover? |
|---|---|---|---|
| **contato** | 2.556 | 29,3% | ⚠️ Sim, se não usar `redes_social` |
| nome_oficial | 760 | 8,7% | Usar na UI |
| nome_popular | 653 | 7,5% | Usar na UI |
| updated_at | 622 | 7,1% | ❓ Se não usa, remover |
| horario_funcionamento | 618 | 7,1% | Usar na UI |
| endereco.logradouro | 471 | 5,4% | Usar na UI |
| subprefeitura | 323 | 3,7% | Usar na UI (filtro) |
| categoria | 286 | 3,3% | Essencial |
| plus10 | 279 | 3,2% | ❓ Se não usa, remover |
| bairro | 249 | 2,9% | Usar na UI |
| regiao_administrativa | 244 | 2,8% | ❓ Se não usa, remover |
| plus8 / plus8_grid | 236 | 2,7% | Essencial (chave) |
| regiao_planejamento | 227 | 2,6% | ❓ Se não usa, remover |
| esfera | 195 | 2,2% | ❓ Se não usa, remover |

---

## Cenários de Otimização

### Cenário A: Status Quo (Atual)
```
Campos: 22
Bytes/chave: 16,2 KB
Redis total: 0,31 GB
Ação: Nada a fazer (espaço não é problema)
```

### Cenário B: Remover não-essenciais
Cortar campos marcados com ❓ (updated_at, plus10, 3 regiões, esfera):
```
Campos: 17
Bytes/chave: ~14 KB (−13%)
Redis total: ~0,28 GB
Benefício: Marginal em espaço; pode melhorar latência (JSON menor)
```

### Cenário C: Remover contato.redes_social
O campo `contato` é RECORD com redes_social, email, telefones, site. Se agente só usa telefones/email:
```
Remover: contato.redes_social + contato.site
Economia: ~1 KB/chave (−6%)
Redis total: ~0,29 GB
Benefício: Marginal
```

### Cenário D: Agressivo (Cortar tudo acima + contato.redes_social)
```
Campos: 15 (core + contato.telefone + contato.email)
Bytes/chave: ~7,5 KB (−54%)
Redis total: ~0,15 GB
Benefício: Reduz Redis; melhora latência
Risco: Remove info que o agente pode precisar
```

---

## Recomendações

### Curto Prazo (Agora)
✅ **Nenhuma ação necessária**. O cache está funcionando e o espaço é negligenciável.

### Médio Prazo (Quando validar uso)
1. **Instrumentar** a tool para saber quais campos o agente **realmente** consulta
2. **Medir** se remover campos melhora tempo de resposta ou reduz tráfego
3. **Se achar bom**, fazer PR removendo `updated_at`, `plus10`, regiões, `esfera`

### Longo Prazo (Se escalar)
- Monitorar **hit rate** do cache (via tracing em `_cache_read`/`_cache_write`)
- Se hit rate cair (muitos plus8 novos por período), considerar cache distribuído ou mais TTL
- Se hit rate subir, validar que espaço Redis não está saturando (é improvável com 0,31 GB)

---

## Como Executar as Análises

Todos os arquivos estão em [sql/bigquery/](../sql/bigquery):

```bash
cd sql/bigquery

# Metadados e dry-run (GRÁTIS):
./inspecionar.sh

# Medições de tamanho (~US$0,002):
./inspecionar.sh --run

# Queries individuais:
bq query --project_id=rj-iplanrio --use_legacy_sql=false \
         --dry_run < q1_equipments_literal.sql

bq query --project_id=rj-iplanrio --use_legacy_sql=false \
         --format=prettyjson < q_sizing_cache_entries.sql

bq query --project_id=rj-iplanrio --use_legacy_sql=false \
         --format=pretty < q_field_weight_breakdown.sql
```

### Pré-requisitos
```bash
gcloud auth application-default login
gcloud config set project rj-iplanrio
bq --version  # >= 2.0
```

---

## Referências no Código

### Cache
- **Implementação:** [src/utils/bigquery.py:1142](../src/utils/bigquery.py#L1142)
  - `_cache_read()` — lê do Redis
  - `_cache_write()` — escreve no Redis com jitter
  - `_ttl_com_jitter()` — variação de ±10% do TTL
  - `_generate_cache_key()` — monta chave semântica

### Queries
- **Q1:** [src/tools/equipments/pluscode_service.py:51](../src/tools/equipments/pluscode_service.py#L51)
- **Q2:** [src/tools/equipments/pluscode_service.py:228](../src/tools/equipments/pluscode_service.py#L228)
- **Q3:** [src/tools/equipments/pluscode_service.py:275](../src/tools/equipments/pluscode_service.py#L275)

### Configuração
- `BIGQUERY_CACHE_TTL_SECONDS` — padrão 3600s
- `REDIS_CACHE_TIMEOUT_SECONDS` — padrão 2s (timeout do cliente async)
- `BIGQUERY_TIMEOUT_SECONDS` — padrão 10s (timeout da query)
- `GOOGLE_BIGQUERY_PAGE_SIZE` — padrão 100 linhas por página

---

## Decisões Registradas

**CHATR-115** (commit [43a3d74](https://github.com/prefeitura-rio/app-mcp-server/commit/43a3d74e59a21776c2dbca53e8ea28ba76db4f3b)):
- Implementou cache Redis em `_cache_read()` / `_cache_write()`
- Chave semântica por namespace + parâmetros + SQL fingerprint
- Single-flight por processo

**CHATR-125** (commit [7cfdd06](https://github.com/prefeitura-rio/app-mcp-server/commit/7cfdd06)):
- Executor dedicado (`_read_executor`) para queries do BigQuery
- Evita que leitura lenta congele geocodificação e logging

---

## Apêndice: Tabelas Referenciadas

| Tabela | Linhas | Tamanho | Clustering | Uso |
|---|---|---|---|---|
| plus_codes.codes | 891.000 | 6,14 GB | categoria | Grade de equipamentos |
| plus_codes.territorio | 2.767 | 12 MB | secretaria_responsavel | Polígonos de território |
| plus_codes.equipamentos_instrucoes | ? | ? | — | Google Sheet (externa) |

---

## Glossário

| Termo | Significado |
|---|---|
| **plus8** | Célula de 8 caracteres da grade plus.codes (~278m × 256m) |
| **Miss** | Requisição que não encontra valor no cache → vai ao BigQuery |
| **Hit** | Requisição que encontra valor no cache → resposta instantânea |
| **TTL** | Time-to-live: 3600s (1 hora) com variação de −10% |
| **Jitter** | Variação aleatória do TTL para desincronizar expiração |
| **Fingerprint** | Hash curto (8 hex) do texto da query para versioning |
| **Namespace** | Prefixo da chave de cache para separar concerns |

