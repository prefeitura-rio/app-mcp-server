# Valor da guia e débitos estruturados na consulta

**Status:** implementado
**Data:** 2026-08-26
**Tipo:** Complemento de contrato (segue o [CHATR-164](CHATR-164-guias-multiplas.md))

Registra como o valor de cada guia emitida e a natureza de cada débito chegam
ao consumidor, já que a PGM não devolve nenhum dos dois.

---

## Contexto

O CHATR-164 fez `emitir_guia` devolver **todas** as guias. Cada item de
`guias_emitidas` levava só o necessário para *pagar*: `pix`,
`codigo_de_barras`, `link` e `data_vencimento`.

Falta o necessário para *entender o que se está pagando*. O consumidor monta um
card de pagamento por guia; com N guias, todos saem idênticos — mesmo layout,
diferindo por um código de barras que não diz nada ao cidadão.

## O que a PGM realmente devolve

Uma tentativa anterior ([PR #161](https://github.com/prefeitura-rio/app-mcp-server/pull/161),
fechado) procurava `valor` e `natureza` no registro da guia, usando listas de
nomes candidatos porque não havia amostra confirmada. **Logs de produção
refutaram a premissa.** A resposta de `v2/guiapagamento/emitir/avista` traz seis
campos:

```python
{'$id': '2', 'dataVencimento': '31/08/2026', 'pdf': 'https://…pdf',
 'arquivoBase64': '<PDF>', 'codigoDeBarras': '81650000048-3 …',
 'codigoQrEMVPix': '000201…54074825.43…'}
```

Sem valor, sem natureza, sem id de guia. `$id` é numeração de objeto do
Json.NET — sequencial dentro da resposta, recomeça do `1` na chamada seguinte.

## Decisão

### 1. O valor vem do código de pagamento, não da PGM

`src/tools/valores_pagamento.py` lê o valor de onde ele já está:

- **PIX (fonte primária):** campo 54 do BR Code (`Transaction Amount`), um
  decimal explícito.
- **Código de barras (fallback):** posições 5-15 do código de arrecadação, em
  centavos — só quando a posição 3 é `6` ou `8` (valor efetivo em Real); `7` e
  `9` indicam valor de referência, que não é dinheiro.

Conferido contra uma guia real: as duas fontes declaram `4825.43`.

O módulo é genérico de propósito — são formatos públicos do BR Code e da
arrecadação FEBRABAN, e qualquer fluxo de emissão de guia pode reusá-lo.

### 2. `valor` é número, e `null` quando não apurável

O consumidor formata no card. `null` diz "não foi possível apurar"; `0.0` diria
"guia sem valor a pagar", que é diferente.

A guia **é sempre entregue**, mesmo sem valor: ela continua pagável, e omiti-la
esconderia do cidadão um débito em aberto. Por isso `valor` é o único campo da
guia que vai ao payload mesmo vazio (`GUIA_CAMPOS_SEMPRE` no workflow) — omitir
a chave faria o consumidor concluir que esta versão não manda o campo.

Isso obrigou `EmitirGuiaResponse.para_dict` a serializar `guias_emitidas` à
parte: `exclude_none=True` é recursivo e apagaria justamente o `valor: None`.

### 3. A natureza não vem da emissão — vem da consulta

O EPGM agrupa os débitos por natureza e emite uma guia por grupo, mas não diz
qual guia corresponde a qual natureza. Em vez de adivinhar pela ordem — que o
EPGM não documenta, e um card com a natureza errada é pior que um sem — a
consulta passa a expor os débitos estruturados, e o consumidor casa por soma:

```json
"itens": [
  {"id": "01/184218/2026-00", "tipo": "cda",  "natureza": "IPTU/Taxas - Predial",
   "natureza_id": "1", "valor": 1922.05},
  {"id": "0334852-76.2017.8.19.0001", "tipo": "ef", "natureza": null,
   "natureza_id": null, "valor": 24897.81},
  {"id": "2026/0009656", "tipo": "guia", "natureza": null,
   "natureza_id": null, "valor": null}
],
"naturezas_divida": ["ISS", "IPTU/Taxas - Predial"]
```

`debitos_msg` continua como estava: serve para montar mensagem, não para o
consumidor processar.

### 4. `naturezas_divida` anda junto de `itens`

Só a **CDA** traz `naturezaDivida` própria. A **EF** traz apenas número e
saldo. A natureza da EF sai por eliminação contra a lista agregada — no exemplo
acima, `ISS` é o que sobra depois que a CDA cobre `IPTU/Taxas - Predial`.

**Isso só é inequívoco quando sobra uma natureza para uma EF.** Com duas EFs de
naturezas diferentes, nem nós nem o consumidor conseguem rotular. Fechar essa
lacuna depende de o EPGM incluir a natureza na resposta.

A guia parcelada não tem natureza nem valor: `valorTotalGuia` veio vazio em
todas as amostras.

### 5. `id` da guia é o GUID do PDF

Não existe id de guia na resposta. O GUID do caminho do PDF
(`…/repositoriorelatorioscertidao/6a13bc0c-….pdf`) é o único identificador
estável: gerado pela PGM, único por guia emitida. **Não serve para consultar a
guia na PGM depois** — o consumidor precisa saber disso.

## Consequências

- Nenhum campo removido ou renomeado. Consumidor que ignora campo desconhecido
  não vê diferença.
- `valor` é o único campo de guia que aparece com valor nulo; os demais seguem
  omitidos quando vazios.
- O texto que vai ao cidadão não muda — os cards são montados pelo consumidor.
- O casamento por soma pressupõe que o valor da guia iguale a soma dos itens da
  natureza. Ambos são calculados para a mesma data de vencimento, mas consulta e
  emissão em datas diferentes divergem por juros; cabe ao consumidor decidir se
  casa por igualdade ou proximidade.

## Onde ficou

| Camada | Arquivo |
| --- | --- |
| Extração de valor (PIX e código de barras) | `src/tools/valores_pagamento.py` |
| Guia e `itens` da consulta (v1) | `src/tools/divida_ativa.py` |
| Modelo da guia (v2) | `src/tools/divida_ativa_v2/models.py` |
| Payload público | `src/tools/multi_step_service/workflows/divida_ativa/divida_ativa_workflow.py` |

## Em aberto

- **Natureza da EF** depende do EPGM incluí-la na resposta de consulta. Até lá,
  o caso de duas EFs com naturezas distintas não tem solução correta.
- **PII nos logs:** `pgm_api` registra a resposta inteira da PGM, que contém
  nome, CPF e endereço do cidadão em claro — hoje indexados no SigNoz. Fora do
  escopo do CHATR-164; precisa de ticket próprio.
