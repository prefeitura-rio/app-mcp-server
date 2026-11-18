#!/usr/bin/env bash
# Grava em src/utils/tool_version.json a versão do merge que disparou o
# update-tool-version.yaml e publica o commit na branch base (DEV-641).
#
# Usado pelo passo "Commit and push version update" do update-tool-version.yaml.
# Entre o checkout do job e o push, a `main` pode andar (outro merge, o commit
# "chore: update staging image" do deploy-staging.yaml), e um `git push` direto
# do checkout sai non-fast-forward. Por isso o arquivo é gravado sobre um
# worktree da ponta atual da branch, e o push é refeito a partir de um fetch
# novo quando perde a corrida. Se nenhuma tentativa entrar, o script falha.
#
# A versão nunca regride: se a ponta já registra a versão deste merge, ou a de
# um merge posterior a ele (o job desse merge chegou antes), não há o que
# commitar.
#
# O checkout de onde o script roda não é alterado.
#
# Variáveis:
#   SOURCE_SHA       commit cuja versão é gravada (padrão: GITHUB_SHA, senão HEAD)
#   VERSION_FILE     padrão: src/utils/tool_version.json
#   REMOTE           padrão: origin
#   BRANCH           padrão: main
#   MAX_ATTEMPTS     padrão: 5
#   RETRY_DELAY      segundos de espera base entre tentativas (cresce a cada uma); padrão: 5

set -euo pipefail

VERSION_FILE="${VERSION_FILE:-src/utils/tool_version.json}"
REMOTE="${REMOTE:-origin}"
BRANCH="${BRANCH:-main}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-5}"
RETRY_DELAY="${RETRY_DELAY:-5}"

fail() {
  echo "::error::$*" >&2
  exit 1
}

if [[ ! "$MAX_ATTEMPTS" =~ ^[1-9][0-9]*$ ]]; then
  fail "MAX_ATTEMPTS deve ser um inteiro positivo"
fi
if [[ ! "$RETRY_DELAY" =~ ^[0-9]+$ ]]; then
  fail "RETRY_DELAY deve ser um inteiro não negativo"
fi

SOURCE_REF="${SOURCE_SHA:-${GITHUB_SHA:-HEAD}}"
SOURCE="$(git rev-parse --verify --quiet "${SOURCE_REF}^{commit}")" \
  || fail "SOURCE_SHA não é um commit deste repositório: ${SOURCE_REF}"
SHORT_SHA="$(git rev-parse --short "$SOURCE")"
VERSION="v${SHORT_SHA}"
COMMIT_MESSAGE="chore: update tool version to ${SHORT_SHA} 🤖"

WORKTREE="$(mktemp -d)"
cleanup() {
  git worktree remove --force "$WORKTREE" >/dev/null 2>&1 || true
  rm -rf "$WORKTREE"
}
trap cleanup EXIT

retry_or_fail() {
  if [ "$attempt" -ge "$MAX_ATTEMPTS" ]; then
    fail "$1 em ${MAX_ATTEMPTS} tentativas; ${VERSION_FILE} não foi atualizado"
  fi
  local delay=$((RETRY_DELAY * attempt))
  echo "$1; nova tentativa em ${delay}s com fetch novo."
  sleep "$delay"
  attempt=$((attempt + 1))
}

# Commit da versão que o arquivo da ponta registra, ou vazio se não houver uma
# que resolva sem ambiguidade.
recorded_commit() {
  local file="$1" short
  [ -f "$file" ] || return 0
  short="$(sed -n '/^[[:space:]]*"version"[[:space:]]*:[[:space:]]*"v\([0-9a-f]\{4,40\}\)".*/{s//\1/p;q;}' "$file")"
  [ -n "$short" ] || return 0
  git rev-parse --verify --quiet "${short}^{commit}" || true
}

attempt=1
while true; do
  echo "Tentativa ${attempt}/${MAX_ATTEMPTS}: ${VERSION_FILE} sobre ${REMOTE}/${BRANCH}"
  if ! git fetch --quiet "$REMOTE" "+refs/heads/${BRANCH}:refs/remotes/${REMOTE}/${BRANCH}"; then
    retry_or_fail "fetch de ${REMOTE}/${BRANCH} falhou"
    continue
  fi
  # SHA explícito: o FETCH_HEAD é de cada worktree.
  tip="$(git rev-parse "refs/remotes/${REMOTE}/${BRANCH}^{commit}")"
  git merge-base --is-ancestor "$SOURCE" "$tip" \
    || fail "${SHORT_SHA} não está em ${REMOTE}/${BRANCH}; ${VERSION_FILE} não foi atualizado"
  if [ ! -e "${WORKTREE}/.git" ]; then
    git worktree add --quiet --detach "$WORKTREE" "$tip"
  else
    git -C "$WORKTREE" reset --quiet --hard "$tip"
  fi

  target="${WORKTREE}/${VERSION_FILE}"
  recorded="$(recorded_commit "$target")"
  if [ "$recorded" = "$SOURCE" ]; then
    echo "${VERSION_FILE} já registra ${VERSION} em ${REMOTE}/${BRANCH}; nada a commitar."
    exit 0
  fi
  if [ -n "$recorded" ] && git merge-base --is-ancestor "$SOURCE" "$recorded"; then
    echo "${VERSION_FILE} já registra $(git rev-parse --short "$recorded"), posterior a ${SHORT_SHA}, em ${REMOTE}/${BRANCH}; nada a commitar."
    exit 0
  fi

  mkdir -p "$(dirname "$target")"
  cat >"$target" <<EOF
{
  "version": "${VERSION}",
  "last_updated": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "description": "Tool version for cache invalidation"
}
EOF
  git -C "$WORKTREE" add -- "$VERSION_FILE"
  if git -C "$WORKTREE" diff --cached --quiet; then
    echo "${VERSION_FILE} já registra ${VERSION} em ${REMOTE}/${BRANCH}; nada a commitar."
    exit 0
  fi

  git -C "$WORKTREE" commit --quiet -m "$COMMIT_MESSAGE"
  if git -C "$WORKTREE" push "$REMOTE" "HEAD:refs/heads/${BRANCH}"; then
    echo "Publicado: $(git -C "$WORKTREE" rev-parse --short HEAD) ${COMMIT_MESSAGE}"
    exit 0
  fi

  retry_or_fail "push de ${VERSION_FILE} para ${REMOTE}/${BRANCH} recusado"
done
