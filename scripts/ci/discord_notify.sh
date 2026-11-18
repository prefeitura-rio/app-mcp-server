#!/usr/bin/env bash
# Envia o aviso de deploy ao Discord (passo "Send Discord notification" do
# deploy-staging.yaml e do release.yaml).
#
# O payload é montado com `jq --arg`, que escapa o texto: mensagem de commit
# ou nome de autor com aspas, barra invertida ou acentos (todo revert tem
# `Revert "..."`) gera JSON válido. Antes ele era montado num heredoc com o
# texto cru, o Discord respondia 400 e o `curl -f` derrubava um deploy que
# tinha dado certo (DEV-629).
#
# O aviso é informativo: falha no envio (HTTP de erro, webhook inalcançável)
# vira `::warning::` e o script sai com 0. Só argumento inválido sai com 2.
#
# Uso:
#   discord_notify.sh --title T --description D --color N \
#     [--field NOME VALOR INLINE]... [--link-field NOME TEXTO URL INLINE]...
#
#   --field       campo com o valor como está.
#   --link-field  campo `[TEXTO](URL)`, com o TEXTO cortado em
#                 DISCORD_LINK_TEXT_MAX caracteres (padrão 50; o corte é por
#                 caractere, não por byte, então não quebra acento).
#   INLINE        true ou false.
#
# Variáveis:
#   DISCORD_WEBHOOK       URL do webhook; vazia = aviso pulado (sai 0).
#   DISCORD_DRY_RUN       1 = só imprime o payload no stdout, sem enviar.
#   DISCORD_TIMEOUT       segundos máximos do envio (padrão 20).
#   DISCORD_LINK_TEXT_MAX padrão 50.

set -euo pipefail

usage() {
  echo "::error::discord_notify.sh: $*" >&2
  exit 2
}

command -v jq >/dev/null 2>&1 || usage "jq não encontrado"

TITLE=""
DESCRIPTION=""
COLOR=""
LINK_TEXT_MAX="${DISCORD_LINK_TEXT_MAX:-50}"
FIELDS='[]'

check_inline() {
  case "$1" in
    true | false) ;;
    *) usage "INLINE deve ser true ou false, recebeu: $1" ;;
  esac
}

[[ "$LINK_TEXT_MAX" =~ ^[0-9]+$ ]] || usage "DISCORD_LINK_TEXT_MAX deve ser inteiro"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --title)
      [ "$#" -ge 2 ] || usage "--title sem valor"
      TITLE="$2"
      shift 2
      ;;
    --description)
      [ "$#" -ge 2 ] || usage "--description sem valor"
      DESCRIPTION="$2"
      shift 2
      ;;
    --color)
      [ "$#" -ge 2 ] || usage "--color sem valor"
      COLOR="$2"
      shift 2
      ;;
    --field)
      [ "$#" -ge 4 ] || usage "--field espera NOME VALOR INLINE"
      check_inline "$4"
      FIELDS=$(jq -c --arg name "$2" --arg value "$3" --argjson inline "$4" \
        '. + [{name: $name, value: $value, inline: $inline}]' <<<"$FIELDS")
      shift 4
      ;;
    --link-field)
      [ "$#" -ge 5 ] || usage "--link-field espera NOME TEXTO URL INLINE"
      check_inline "$5"
      FIELDS=$(jq -c --arg name "$2" --arg text "$3" --arg url "$4" \
        --argjson inline "$5" --argjson max "$LINK_TEXT_MAX" \
        '. + [{name: $name, value: ("[" + $text[0:$max] + "](" + $url + ")"), inline: $inline}]' \
        <<<"$FIELDS")
      shift 5
      ;;
    *)
      usage "argumento desconhecido: $1"
      ;;
  esac
done

[ -n "$TITLE" ] || usage "--title é obrigatório"
[[ "$COLOR" =~ ^[0-9]+$ ]] || usage "--color deve ser inteiro"

PAYLOAD=$(jq -n \
  --arg title "$TITLE" \
  --arg description "$DESCRIPTION" \
  --argjson color "$COLOR" \
  --argjson fields "$FIELDS" \
  --arg timestamp "$(date -u +%Y-%m-%dT%H:%M:%S.000Z)" \
  '{embeds: [{
      title: $title,
      description: $description,
      color: $color,
      fields: $fields,
      timestamp: $timestamp,
      footer: {text: "GitHub Actions"}
    }]}')

if [ "${DISCORD_DRY_RUN:-}" = "1" ]; then
  printf '%s\n' "$PAYLOAD"
  exit 0
fi

if [ -z "${DISCORD_WEBHOOK:-}" ]; then
  echo "Discord webhook not configured, skipping notification."
  exit 0
fi

if curl -fsS --max-time "${DISCORD_TIMEOUT:-20}" \
  -H "Content-Type: application/json" \
  --data-binary @- \
  -o /dev/null \
  "$DISCORD_WEBHOOK" <<<"$PAYLOAD"; then
  echo "Discord notification sent."
else
  status=$?
  # A URL do webhook é segredo: não entra na mensagem.
  echo "::warning::Discord notification failed (curl exit ${status}); the deploy result is unchanged."
fi
exit 0
