# WhatsApp Flow JSON canonical sources

Cada `*.flow.json` aqui é a fonte canônica do Flow correspondente publicado
no WhatsApp Business Platform. Versionar em git habilita:

- Histórico de mudanças auditável
- Rollback rápido (re-upload do snapshot anterior)
- Code review do template antes do republish
- Bootstrap de Flow novo (clone + edit)

## Flows ativos

| Arquivo | Flow ID Meta | Categoria | Tipo |
|---|---|---|---|
| `reparo_luminaria.flow.json` | `4141008006029185` | CUSTOMER_SUPPORT | dinâmico (`data_api_version: 3.0`); prefill via `flow_token` (defect/qty/location/endereco) |

## Como atualizar um Flow publicado

```bash
# 1. Editar o JSON localmente
# 2. Validar via API (status=DRAFT, validation_errors=[])
curl -X POST \
  -H "Authorization: Bearer $META_TOKEN" \
  "https://graph.facebook.com/v21.0/$FLOW_ID/assets" \
  -F "name=flow.json" \
  -F "asset_type=FLOW_JSON" \
  -F "file=@<flow>.flow.json;type=application/json"

# 3. Republish se validation passou
curl -X POST -H "Authorization: Bearer $META_TOKEN" \
  "https://graph.facebook.com/v21.0/$FLOW_ID/publish"
```

## Prefill via Form `init-values`

Padrão usado em todos os Flows estáticos deste diretório:

```json
{
  "type": "Form",
  "name": "form",
  "init-values": {
    "<field_name>": "${data.<field>_prefill}"
  },
  "children": [
    {"type": "TextInput", "name": "<field_name>", "label": "..."}
  ]
}
```

`screen.data` declara `<field>_prefill` com `type` e `__example__`. O bot
encoda os valores em `flow_token` (formato `v1:base64(json)`); o endpoint
MCP `_handle_init` decoda e retorna no INIT response. Detalhes em
`src/tools/luminaria_entity_extractor.py`.

## Constraints aprendidos empiricamente (validados via API)

- `init-value` direto em TextInput/RadioButtonsGroup → REJEITADO em v7.3
  com Form. Use sempre `init-values` (plural) no Form parent.
- Operadores `||`, `in` em `If`/`Switch` conditions → não aceitos como
  documentados. Single `==` funciona, OR exige Switch.
- Switch com nomes de form components duplicados em múltiplos `cases` →
  rejeitado (`DUPLICATE_FORM_COMPONENT_NAMES`).
- `data_api_version: 3.0` torna Flow dinâmico: cliente sobrescreve
  `flow_action_payload.data` com a resposta do endpoint server no INIT.
  Pra prefill funcionar visualmente, o endpoint deve refletir o data
  recebido na response, OU remover `data_api_version` (Flow estático).
