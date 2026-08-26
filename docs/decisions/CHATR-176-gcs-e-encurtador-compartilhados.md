# Upload no GCS e encurtamento de URL viram utilitários compartilhados

**Status:** implementado
**Data:** 2026-08-26
**Tipo:** Débito técnico (extração + correções que a extração habilitou)

Registra a extração do caminho "conteúdo → GCS → signed URL → link curto" de
dentro do workflow de IPTU para `src/utils/`.

---

## Contexto

O IPTU entrega dois links ao cidadão: a página Pix e o PDF do DARM. Nenhum dos
dois existe como URL antes da emissão — a página é montada na hora, e o PDF
chega da API da Prefeitura em base64. Os dois seguem o mesmo caminho: sobem para
o bucket de workflows, saem como signed URL, e passam pelo encurtador interno
porque uma signed URL não cabe numa conversa de WhatsApp.

Esse caminho estava escrito **duas vezes**, em `iptu/api/api_service.py` e em
`iptu/pix_page_service.py`:

| Trecho | `api_service.py` | `pix_page_service.py` |
| --- | --- | --- |
| `get_credentials_from_env` | 851-856 | 39-41 |
| `get_short_url` | 858-915 | 79-127 |
| Upload + signed URL | 821-849 | 43-57 |
| Formatação de `expires_at` | 590-593 (inline) | 26-32 |

As duas versões de `get_short_url` eram idênticas, exceto por defaults de
`title`/`description` específicos de IPTU numa delas.

## Por que agora

**CHATR-174 (item D3) corrige uma linha que existe nos dois arquivos** — o
`logger.info(f"URL shortened successfully: {data}")`, que registra no SigNoz o
`short_path`, ou seja, o link direto para a guia daquele contribuinte. A própria
issue observa: *"Duplicado nos dois arquivos — corrigir juntos"*. Com a extração
feita antes, D3 vira uma correção só.

**E é o que bloqueia o reuso pela Dívida Ativa.** Quando a PGM não devolve a URL
do PDF, o workflow despeja o PDF inteiro em base64 dentro da mensagem
(`arquivoBase64`, via `GUIA_CAMPOS_MENSAGEM` — CHATR-164). O mecanismo que
resolve isso já existia, mas estava preso dentro do IPTU.

## Decisão

### 1. Dois módulos, não um

`gcs_storage` e `short_url` são integrações com serviços diferentes, e o segundo
não depende do primeiro — qualquer URL pode ser encurtada, venha ela do GCS ou
não. Separá-los segue o que `src/utils/` já faz: um arquivo por integração
externa (`bigquery.py`, `typesense_api.py`, `infisical.py`, `http_client.py`).

### 2. O que é política do fluxo entra por parâmetro

O helper de upload não decide caminho do blob, content-type nem validade. Isso é
de quem chama: a página Pix vale 24h (`PIX_PAGE_TTL_HOURS`, no
`pix_page_service`), o PDF do DARM vale 7 dias (`PDF_DARM_TTL`, no
`api_service`).

**Os dois TTLs continuam divergentes de propósito.** Unificá-los seria mudança
de comportamento, fora do escopo deste ticket — mas a divergência, que antes
estava enterrada em dois `timedelta` literais, agora é uma constante nomeada em
cada chamador.

### 3. `source` do interceptor é parâmetro, não derivado

`get_short_url` recebe o dict de `source` inteiro em vez de um `workflow: str`.
O IPTU usa `{"source": "mcp", "tool": "multi_step_service", "workflow": "iptu_pagamento"}`,
mas a Dívida Ativa usa outro formato (`{"source": "mcp", "tool": "divida_ativa"}`,
nos decorators de `divida_ativa.py`). Fixar a forma agora engessaria o helper
antes de existir um segundo consumidor.

Na mesma linha, os defaults de `title`/`description` foram removidos: eram
strings de IPTU ("PDF para pagamento de cotas do IPTU") num helper que não é de
IPTU. O único chamador que os usava por omissão passa a explicitá-los.

### 4. A decodificação do base64 fica com o chamador

`blob.upload_from_string` aceita `str` e `bytes`, então um helper só serve para o
HTML da página Pix e para o PDF. Só um chamador tem base64 na mão, e é ele quem
decodifica — o helper não adivinha o encoding do que recebe.

### 5. Com um lugar só, três correções deixaram de ser caras

A extração era pré-requisito para elas: nos dois arquivos, cada uma custaria o
dobro e correria o risco de divergir. Feitas aqui:

**O `short_path` saiu do log** (CHATR-174 D3). A resposta do encurtador traz o
link direto para a guia daquele contribuinte — quem lê o SigNoz abre o
documento. O `logger.info` agora não registra o corpo da resposta.

**A signed URL do GCS deixou de chegar em claro ao interceptor de erros.** Ela é
uma capability: quem tem a URL baixa o arquivo, sem autenticar. Como o
encurtador a recebe no campo `destination`, qualquer 4xx/5xx dele reportava o
payload inteiro ao monitoramento. `SENSITIVE_KEYS` em `http_client.py` ganhou
`signature`, `x-goog-credential` e `googleaccessid`, o que cobre signed URL v2 e
v4 — sem a assinatura o GCS recusa a URL, e o caminho do blob continua legível
para diagnóstico.

**O upload saiu do event loop.** `upload_from_string` e `generate_signed_url`
são síncronas, e montar o client ainda parseia a chave RSA da service account:
com o PDF de um contribuinte subindo, todos os outros atendimentos ficavam
parados. A parte bloqueante virou `_upload_sync`, chamada por
`asyncio.to_thread`. A assinatura pública de `upload_to_gcs` não mudou.

Além delas, `format_expires_at` passou a exigir `tzinfo`: com datetime naive, o
`astimezone` assumiria o fuso da máquina e o vencimento do link passaria a
depender do TZ do container. Nenhum chamador atual passa naive — o guard existe
porque o helper vai ganhar um segundo consumidor.

## Consequências

- **Os links não mudaram.** Blob path, content-type, TTL, título, descrição e
  formato do `expires_at` saem idênticos aos de antes, nos dois. O que mudou de
  comportamento está todo em observabilidade e concorrência: o que vai ao log,
  o que vai ao interceptor, e em que thread o upload roda.
- `ERROR_SOURCE` no `api_service` substitui **quatro** dicts inline idênticos
  (três em `InterceptedHTTPClient`, um novo no encurtador).
- Os testes do encurtador e do upload saíram de `test_iptu_api_service.py` para
  `src/tests/unit/utils/`. O que sobrou no teste de IPTU é o que é de IPTU:
  onde o arquivo é gravado, com que validade e com que texto vai ao encurtador.
- A suíte de IPTU carrega módulos por caminho (`load_module`) e faz monkeypatch
  no namespace onde a função está definida. Mover código entre módulos quebra
  esses patches — os testes afetados passaram a mirar a função importada no
  namespace do chamador.

## Onde ficou

| Camada | Arquivo |
| --- | --- |
| Upload no bucket de workflows e signed URL | `src/utils/gcs_storage.py` |
| Encurtador da Prefeitura e formato de vencimento | `src/utils/short_url.py` |
| Política do link da página Pix (24h) | `iptu_pagamento/pix_page_service.py` |
| Política do link do PDF do DARM (7 dias) | `iptu_pagamento/api/api_service.py` |
| Redação da signed URL antes do interceptor | `src/utils/http_client.py` (`SENSITIVE_KEYS`) |
| Testes dos helpers | `src/tests/unit/utils/test_{gcs_storage,short_url}.py` |

## Em aberto

- **`arquivoBase64` da Dívida Ativa** — o consumidor natural destes helpers.
  Quando for feito, o link curto entra **só na mensagem**, não no payload
  público: o CHATR-164 registrou que o payload volta a cada passo do workflow.
- **Página Pix para a Dívida Ativa** — exige gerar o QR a partir do EMV
  (`codigoQrEMVPix`), enquanto `build_pix_copy_page` hoje espera a imagem em
  base64 que só o IPTU manda (`QrCodePIX`). Precisa de dependência nova
  (`segno`/`qrcode`) e de parametrizar o título, fixo em "Pix IPTU".
- **Client do GCS por chamada** — `get_workflows_bucket` monta um
  `storage.Client` e parseia a chave RSA a cada upload. Fora do event loop isso
  deixou de travar o atendimento, mas segue sendo trabalho repetido; cachear
  muda o comportamento quando a service account rotaciona, então fica para
  quando houver volume que justifique.
