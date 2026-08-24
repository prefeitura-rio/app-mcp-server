# Valor e natureza por guia emitida

**Status:** implementado, com um ponto aberto (nomes dos campos na PGM)
**Data:** 2026-08-24
**Tipo:** Complemento de contrato (segue o [CHATR-164](CHATR-164-guias-multiplas.md))

Registra a inclusão do valor em dinheiro e da natureza do débito em cada item
de `guias_emitidas`.

---

## Contexto

O CHATR-164 fez a resposta de `emitir_guia` devolver **todas** as guias, e não
só a última. Cada item de `guias_emitidas` levava apenas o que é preciso para
*pagar*: `pix`, `codigo_de_barras`, `link` e `data_vencimento`.

Falta o que é preciso para *entender o que se está pagando*. O consumidor
monta um card de pagamento por guia; com N guias, todos os cards saem
idênticos — mesmo layout, mesma ausência de rótulo, diferindo só por um código
de barras que não diz nada ao cidadão. O EPGM agrupa por natureza de débito,
então "guia 1" e "guia 2" são exatamente a distinção que o cidadão precisa ver:
qual débito, quanto custa.

## Decisão

Mudança **aditiva**, nos mesmos quatro pontos do CHATR-164.

### 1. `valor` e `natureza` em cada guia

```json
{
  "total_guias": 2,
  "guias_emitidas": [
    {"pix": "pix-1", "codigo_de_barras": "111", "link": "a.pdf",
     "data_vencimento": "10/04/2026", "valor": "R$ 1.234,50", "natureza": "cda"},
    {"pix": "pix-2", "codigo_de_barras": "222", "link": "b.pdf",
     "data_vencimento": "11/04/2026", "valor": "R$ 99,00", "natureza": "auto_infracao"}
  ]
}
```

### 2. Não sobem para os campos no topo

Os quatro escalares no topo existem para os consumidores anteriores ao
CHATR-164, que leem uma guia só. `valor` e `natureza` nascem já dentro de
`guias_emitidas` e não têm consumidor antigo para preservar — duplicá-los no
topo criaria legado novo no mesmo commit que o cria.

A exceção é o payload público do workflow (`guia_pagamento_a_vista`), onde o
topo é uma cópia da primeira guia e os dois campos vêm junto por construção.

### 3. Valor sempre texto, formatado em BRL

A consulta de débitos da PGM devolve valor já formatado (`"R$5.000,00"`); não
sabemos se a emissão faz o mesmo. Número vira `"R$ 1.234,50"` na extração, para
o consumidor não ter que descobrir em qual dos dois formatos o campo chegou.

### 4. O nome dos campos na PGM é procurado numa lista de candidatos

**Este é o ponto aberto.** Os quatro campos de pagamento têm nome confirmado
(`codigoDeBarras`, `pdf`, `dataVencimento`, `codigoQrEMVPix`) — vieram de
resposta real. Valor e natureza, não: a resposta de emissão da PGM não é
documentada e nunca precisamos desses dois antes, então não há amostra que
mostre como se chamam.

`CAMPOS_PGM_VALOR` e `CAMPOS_PGM_NATUREZA` listam os nomes plausíveis,
deduzidos dos endpoints de consulta da mesma API (`valorSaldoTotal`,
`valorTotalGuia`, `naturezaDivida`). Vale o primeiro preenchido. É o mesmo
padrão que `GUIA_CAMPOS` já usa no workflow para aceitar `dataVencimento` ao
lado de `data_vencimento`.

### 5. Registro sem nenhum dos dois é logado

`guia_sem_valor_nem_natureza` registra **as chaves** que a PGM enviou — nunca
os valores, que carregam o PDF inteiro em base64. É esse log que fecha o ponto
aberto: a primeira emissão real em staging mostra o nome verdadeiro dos campos,
e a lista de candidatos encolhe para ele.

Se a PGM não devolver nenhum dos dois por guia, o log é o que prova isso — e aí
a informação precisa vir de outra fonte, porque o wrapper não tem como
atribuir uma natureza a uma guia depois que o EPGM agrupou os débitos.

## Consequências

- Consumidor que ignora campo desconhecido não vê diferença; guia sem valor e
  sem natureza continua com os quatro campos de pagamento de sempre.
- Campo ausente vem como `""`, e não omitido — mesma escolha do CHATR-164 para
  os quatro originais.
- No payload público do workflow, campo vazio é **omitido** (`_normalizar_guia`
  descarta falsy), então o payload de hoje não ganha chave vazia nenhuma.
- O texto que vai ao cidadão não muda: nem o de guia única, nem o de N guias.
  Os cards são montados pelo consumidor, a partir do payload.

## Onde ficou

| Camada | Arquivo |
| --- | --- |
| Contrato da PGM (candidatos, formatação, log) | `src/tools/divida_ativa.py` |
| Extração v1 (`_extrair_guias`) | `src/tools/divida_ativa.py` |
| Modelo v2 (`GuiaEmitida`) | `src/tools/divida_ativa_v2/models.py` |
| Payload público do workflow (`GUIA_CAMPOS`) | `src/tools/multi_step_service/workflows/divida_ativa/divida_ativa_workflow.py` |
