# CHATR-164 — Resposta de `emitir_guia` devolve todas as guias emitidas

**Status:** decidido e implementado
**Data:** 2026-08-21
**Tipo:** Bug (correção de contrato de resposta)

Registra a correção do achatamento da resposta de emissão de guias: até aqui,
uma solicitação que gerava N guias devolvia apenas uma.

---

## Contexto

O EPGM emite **uma guia por natureza de débito**. Uma única chamada de
`emitir_guia` com N identificadores (`cdas`, `efs` ou `guias`) pode, portanto,
devolver N registros — cada um com seu PIX, código de barras, PDF e vencimento.

A resposta do wrapper comportava uma guia só. O código montava os campos
iterando os registros e reatribuindo as mesmas chaves a cada volta:

```python
for item in registros:
    message["codigo_de_barras"] = item.get("codigoDeBarras") or ""
    message["link"] = item.get("pdf") or ""
    ...
```

Resultado: sobrevivia o **último** registro; os demais desapareciam sem sinal
na resposta. No fluxo de Dívida Ativa, o cidadão recebia um PIX, pagava, e
achava que tinha quitado tudo o que selecionou — enquanto os outros débitos
seguiam em aberto, com juros correndo e risco de protesto.

### O que causava o quê

1. **Loop que sobrescreve** — `processar_registros` (v1) e `_extrair_dados_guia`
   (v2). O sintoma direto.
2. **Contrato 1:1** — `EmitirGuiaResponse` declarava `codigo_de_barras`, `link`,
   `data_vencimento` e `pix` como escalares, com `extra="forbid"`. Corrigir o
   loop sozinho não resolveria: não havia campo de destino. Foi por isso que a
   v2 se limitou a logar `emitir_guia_v2_multiplos_registros` e seguir
   devolvendo um registro.
3. **A cardinalidade N sempre veio da PGM** — `pgm_api` devolve
   `response.get("data")`, uma lista; a entrada já enviava listas de
   identificadores. O 1:N está na API upstream desde sempre.
4. **Não é regressão da v2** — o padrão existia na v1 desde a origem do arquivo.
   A v2 portou a semântica de propósito ("o último registro vence").
5. **Só apareceu quando o fluxo passou a permitir multi-seleção** — com
   `apenas_um_item`, o achatamento era inofensivo; com `itens_informados` como
   lista, virou perda de dados.
6. **O consumidor não tinha como detectar** — a resposta traz `cdas`/`efs`/
   `guias`, mas são os identificadores *enviados*. Como o EPGM agrupa por
   natureza, `len(cdas)` ≠ nº de guias: comparar tamanhos daria falso positivo
   em qualquer seleção agrupada.

## Decisão

Mudança **aditiva** de contrato, aplicada na v1 e na v2.

### 1. `guias_emitidas` + `total_guias`

A resposta passa a trazer todas as guias:

```json
{
  "api_resposta_sucesso": true,
  "total_guias": 2,
  "guias_emitidas": [
    {"codigo_de_barras": "111", "link": "a.pdf", "data_vencimento": "10/04/2026", "pix": "pix-1"},
    {"codigo_de_barras": "222", "link": "b.pdf", "data_vencimento": "11/04/2026", "pix": "pix-2"}
  ],
  "codigo_de_barras": "111",
  "link": "a.pdf",
  "data_vencimento": "10/04/2026",
  "pix": "pix-1"
}
```

Na v2 o item é o modelo `GuiaEmitida`, com `extra="ignore"` para tolerar campo
novo da PGM sem transformar emissão bem-sucedida em erro.

### 2. Os campos escalares continuam existindo — agora com a primeira guia

Nada foi removido: os consumidores que hoje leem uma guia só seguem
funcionando, e migram para `guias_emitidas` quando quiserem.

Passam a refletir a **primeira** guia, não a última. "Última vence" era efeito
colateral do loop, não escolha: não corresponde a nenhuma ordem que o cidadão
veja. A primeira é estável e corresponde à ordem devolvida pela PGM.

### 3. Nome do campo: `guias_emitidas`, não `guias`

`guias` já existe na resposta com outro significado — os identificadores de
guia enviados para regularização. Reusar o nome quebraria consumidor.

### 4. Resposta sem nenhuma guia passa a ser erro

Antes, registros vazios produziam `api_resposta_sucesso: true` sem nenhum campo
de guia — o consumidor recebia sucesso sem nada para o cidadão pagar. Agora
devolve `api_resposta_sucesso: false` com `MENSAGEM_SEM_GUIA`, e loga
`emitir_guia_v2_sem_guia` / `processar_registros_sem_guia`.

### 5. Registro único fora de lista é aceito

Se a PGM devolver o registro solto em vez de uma lista de um, ele agora vira
uma guia. Na v2 esse caso caía no teste de tipo e produzia resposta sem guia
nenhuma; na v1 estourava `AttributeError`.

### 6. A v1 foi corrigida junto

Não é só compatibilidade: o workflow `multi_step` chama a **v1**, não a v2
(`workflows/divida_ativa/api_service.py`). Corrigir só a v2 deixaria esse fluxo
com o mesmo bug.

### 7. O workflow entrega as N guias na conversa

`_guias_da_resposta` normaliza a resposta da tool (com fallback para os campos
no topo, cobrindo resposta antiga em cache de conversa) e os templates passam a
receber a lista. Com uma guia, o texto ao cidadão é **exatamente o de antes**.
Com N, vem precedido do aviso de que foram geradas N guias e que todas precisam
ser pagas, seguido de um bloco por guia com o vencimento.

O payload público (`guia_pagamento_a_vista`) ganha `guias` e `total_guias`,
mantendo os campos no topo como a primeira guia.

## Consequências

- Consumidores que leem apenas os campos escalares **não quebram**, mas seguem
  vendo uma guia só: precisam migrar para `guias_emitidas` para não induzir o
  cidadão a pagar parcialmente.
- Quem depender de "último registro vence" verá mudança de valor — não há
  consumidor conhecido nessa situação, e o comportamento anterior era o bug.
- Emissão que devolvia sucesso vazio agora devolve erro: é mudança visível de
  status para um caso que já era inútil ao cidadão.
- O payload público e o texto ao cidadão usam mappings de campo separados
  (`GUIA_CAMPOS` e `GUIA_CAMPOS_MENSAGEM`). Só o segundo aceita `arquivoBase64`
  como origem do `link`: é o PDF inteiro em base64, aceitável numa mensagem
  pontual e não no payload que volta a cada passo do workflow.

## Onde ficou

| Camada | Arquivo |
| --- | --- |
| Modelo v2 (`GuiaEmitida`, campos novos) | `src/tools/divida_ativa_v2/models.py` |
| Extração v2 (`_extrair_guias`) | `src/tools/divida_ativa_v2/service.py` |
| v1 (`_extrair_guias`, `processar_registros`, constantes da PGM) | `src/tools/divida_ativa.py` |
| Workflow (`_guias_da_resposta`, payload público) | `src/tools/multi_step_service/workflows/divida_ativa/divida_ativa_workflow.py` |
| Templates (1 e N guias) | `src/tools/multi_step_service/workflows/divida_ativa/templates.py` |
