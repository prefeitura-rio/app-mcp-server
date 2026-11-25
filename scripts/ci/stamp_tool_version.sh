#!/usr/bin/env bash
# Grava em src/utils/tool_version.json a versão de um commit, sem commitar
# (DEV-794).
#
# Usado pelo build-docker.yaml antes do `docker build`: a imagem de um merge
# leva a versão desse merge. Sem isso, ela levava o arquivo da árvore do
# próprio merge, que registra o merge anterior — o bump deste só entra num
# commit seguinte do update-tool-version.yaml, que nunca vira imagem.
#
# Também usado pelo commit_tool_version.sh, para o arquivo da main e o da
# imagem do mesmo merge serem idênticos: `version` = v<SHA curto do commit> e
# `last_updated` = data do commit (UTC), e não a hora em que o job rodou.
#
# Variáveis:
#   SOURCE_SHA    commit cuja versão é gravada (padrão: GITHUB_SHA, senão HEAD)
#   OUTPUT        arquivo gravado (padrão: src/utils/tool_version.json)

set -euo pipefail

OUTPUT="${OUTPUT:-src/utils/tool_version.json}"

fail() {
  echo "::error::$*" >&2
  exit 1
}

SOURCE_REF="${SOURCE_SHA:-${GITHUB_SHA:-HEAD}}"
SOURCE="$(git rev-parse --verify --quiet "${SOURCE_REF}^{commit}")" \
  || fail "SOURCE_SHA não é um commit deste repositório: ${SOURCE_REF}"
SHORT_SHA="$(git rev-parse --short "$SOURCE")"
VERSION="v${SHORT_SHA}"
UPDATED="$(TZ=UTC git show -s --format=%cd --date=format-local:%Y-%m-%dT%H:%M:%SZ "$SOURCE")"

mkdir -p "$(dirname "$OUTPUT")"
cat >"$OUTPUT" <<EOF
{
  "version": "${VERSION}",
  "last_updated": "${UPDATED}",
  "description": "Tool version for cache invalidation"
}
EOF
echo "${OUTPUT}: ${VERSION} (${UPDATED})"
