#!/usr/bin/env bash
# Grava em k8s/staging/resources.yaml a imagem que o deploy de staging vai
# implantar e publica o commit na branch base (DEV-412).
#
# Usado pelo passo "Commit resolved image to git" do deploy-staging.yaml.
# Depois de cada merge, o update-tool-version.yaml empurra um commit na `main`
# enquanto o deploy ainda espera a imagem; um `git push` direto do checkout do
# SHA implantado sai non-fast-forward. Por isso o manifesto é editado sobre um
# worktree da ponta atual da branch, e o push é refeito a partir de um fetch
# novo quando perde a corrida. Se nenhuma tentativa entrar, o script falha: o
# registro não se perde em silêncio e o deploy para antes de mexer no cluster.
#
# O checkout de onde o script roda (o SHA implantado, usado depois pelos E2E)
# não é alterado.
#
# Variáveis:
#   FULL_IMAGE       obrigatória: ghcr.io/prefeitura-rio/app-mcp-server:<tag>@sha256:<digest>
#   DEPLOYED_SHA     commit implantado, para a mensagem (padrão: GITHUB_SHA, senão HEAD)
#   MANIFEST         padrão: k8s/staging/resources.yaml
#   REMOTE           padrão: origin
#   BRANCH           padrão: main
#   MAX_ATTEMPTS     padrão: 5
#   RETRY_DELAY      segundos de espera base entre tentativas (cresce a cada uma); padrão: 5

set -euo pipefail

MANIFEST="${MANIFEST:-k8s/staging/resources.yaml}"
REMOTE="${REMOTE:-origin}"
BRANCH="${BRANCH:-main}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-5}"
RETRY_DELAY="${RETRY_DELAY:-5}"
IMAGE_REPO="ghcr.io/prefeitura-rio/app-mcp-server"

fail() {
  echo "::error::$*" >&2
  exit 1
}

FULL_IMAGE="${FULL_IMAGE:-}"
if [[ ! "$FULL_IMAGE" =~ ^ghcr\.io/prefeitura-rio/app-mcp-server:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}$ ]]; then
  fail "FULL_IMAGE inválida ou vazia: esperado ${IMAGE_REPO}:<tag>@sha256:<digest>"
fi
if [[ ! "$MAX_ATTEMPTS" =~ ^[1-9][0-9]*$ ]]; then
  fail "MAX_ATTEMPTS deve ser um inteiro positivo"
fi
if [[ ! "$RETRY_DELAY" =~ ^[0-9]+$ ]]; then
  fail "RETRY_DELAY deve ser um inteiro não negativo"
fi

DEPLOYED_SHA="${DEPLOYED_SHA:-${GITHUB_SHA:-$(git rev-parse HEAD)}}"
SHORT_SHA="$(git rev-parse --short "${DEPLOYED_SHA}^{commit}")"
COMMIT_MESSAGE="chore: update staging image to ${SHORT_SHA}"
IMAGE_LINE="image: ${FULL_IMAGE}  # Updated by deploy-staging workflow"

WORKTREE="$(mktemp -d)"
cleanup() {
  git worktree remove --force "$WORKTREE" >/dev/null 2>&1 || true
  rm -rf "$WORKTREE"
}
trap cleanup EXIT

attempt=1
while true; do
  echo "Tentativa ${attempt}/${MAX_ATTEMPTS}: ${MANIFEST} sobre ${REMOTE}/${BRANCH}"
  git fetch --quiet "$REMOTE" "+refs/heads/${BRANCH}:refs/remotes/${REMOTE}/${BRANCH}"
  # SHA explícito: o FETCH_HEAD é de cada worktree.
  tip="$(git rev-parse "refs/remotes/${REMOTE}/${BRANCH}^{commit}")"
  if [ "$attempt" -eq 1 ]; then
    git worktree add --quiet --detach "$WORKTREE" "$tip"
  else
    git -C "$WORKTREE" reset --quiet --hard "$tip"
  fi

  target="${WORKTREE}/${MANIFEST}"
  [ -f "$target" ] || fail "${MANIFEST} não existe em ${REMOTE}/${BRANCH}"
  grep -qF "image: ${IMAGE_REPO}:" "$target" \
    || fail "${MANIFEST} não tem a linha de imagem de ${IMAGE_REPO}"

  sed -i "s|image: ${IMAGE_REPO}:.*|${IMAGE_LINE}|" "$target"
  git -C "$WORKTREE" add -- "$MANIFEST"
  if git -C "$WORKTREE" diff --cached --quiet; then
    echo "${MANIFEST} já referencia ${FULL_IMAGE} em ${REMOTE}/${BRANCH}; nada a commitar."
    exit 0
  fi

  git -C "$WORKTREE" commit --quiet -m "$COMMIT_MESSAGE"
  if git -C "$WORKTREE" push "$REMOTE" "HEAD:refs/heads/${BRANCH}"; then
    echo "Publicado: $(git -C "$WORKTREE" rev-parse --short HEAD) ${COMMIT_MESSAGE}"
    exit 0
  fi

  if [ "$attempt" -ge "$MAX_ATTEMPTS" ]; then
    fail "push de ${MANIFEST} para ${REMOTE}/${BRANCH} recusado em ${MAX_ATTEMPTS} tentativas; o manifesto não foi atualizado"
  fi
  delay=$((RETRY_DELAY * attempt))
  echo "Push recusado (a branch andou?); nova tentativa em ${delay}s com fetch novo."
  sleep "$delay"
  attempt=$((attempt + 1))
done
