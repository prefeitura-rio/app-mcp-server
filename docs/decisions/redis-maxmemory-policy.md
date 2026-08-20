# Redis maxmemory-policy — Análise e Decisão Pendente

**Data**: 2026-08-19  
**Status**: Análise concluída, decisão pendente  
**Afetados**: Deployment `mcp-redis` em `k8s/infra`

## Achado

A configuração do Redis no cluster **não define `maxmemory` ou `maxmemory-policy**. O container usa defaults compilados no binário:

- `maxmemory = 0` (sem limite)
- `maxmemory-policy = noeviction` (padrão)

Como `maxmemory = 0`, a policy é inoperante: o Redis cresce livremente até bater no `limits.memory = 512Mi` do container e é **OOMKilled pelo kubelet**, perdendo tudo.

## Diagnóstico atual

Chaves escritas no Redis:

| Chave | Origem | TTL | Persistência | Comportamento na falha |
| --- | --- | --- | --- | --- |
| `bq_dlq:*` | [bigquery.py:184](../../src/utils/bigquery.py#L184) `rpush` | Nenhum | Persistente | Cai para arquivo local (`DATA_DIR/bq_dlq/*.jsonl`) |
| Cache BigQuery | [bigquery.py:1230](../../src/utils/bigquery.py#L1230) `setex` | ✓ ~1h | Reconstruível | Leitura retorna `miss`, query roda novamente |
| Estado multi-step | [state.py:153](../../src/tools/multi_step_service/core/state.py#L153) `set ... ex=ttl` | ✓ | Reconstruível | ⚠️ **Sem try/except** — OOM quebraria o fluxo |

Hoje: **perda total do dataset** ao atingir memória (OOMKill).

## Opções de Policy

### 1. `noeviction` (atual)

```hcl
--maxmemory "320mb"
--maxmemory-policy "noeviction"
```

**Comportamento**: Escritas retornam `OOM command not allowed` ao encher.

**Pros**:
- Nunca perde dados
- Cache e DLQ persistem até expiração/acesso

**Contras**:
- `save_user_data` (multi-step) **quebraria com erro OOM** (sem try/except)
- Pressão resolvida apenas por expiração de TTL (~1h)

### 2. `volatile-lru` (recomendado)

```hcl
--maxmemory "320mb"
--maxmemory-policy "volatile-lru"
```

**Comportamento**: Despeja chaves com TTL, menos usadas recentemente.

**Pros**:
- Preserva `bq_dlq:*` (chaves sem TTL)
- Sacrifica só cache (reconstruível)
- Multi-step continua funcionando
- Redis nunca recusa escritas

**Contras**:
- Cache pode ser despejado mais agressivamente

---

## Alerta: PDB + Replica

**Achado adjacente** ([mcp_redis_pdb](../../k8s/infra/mcp_redis.tf#L...)): 
- `minAvailable = 1` com `replicas = 1` bloqueia drain de node
- Upgrade do cluster trava até deletar PDB na mão

---

## Verificação

```bash
kubectl exec -n mcp deploy/mcp-redis -- sh -c \
  'redis-cli -a "$REDIS_PASSWORD" CONFIG GET maxmemory maxmemory-policy'
```

Deve retornar `0` e `noeviction` (defaults).

---

## Próximos Passos

1. **Decidir entre opções** → qual nível de perda é aceitável?
   - DLQ é crítico? (volumoso? frequente?)
   - Multi-step quebra com erro OOM é problema?
   
2. **Se `volatile-lru`**: ajustar Terraform (`k8s/infra/mcp_redis.tf`) command args

3. **Considerar persistência**: se `bq_dlq:*` é crítico, volume + RDB/AOF valem

4. **Monitorar**: adicionar alerta quando `used_memory` > 70% de `maxmemory`
