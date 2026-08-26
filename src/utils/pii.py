"""
Redação de PII e de credenciais (CHATR-167)

Fonte única dos padrões e das máscaras. Antes deste módulo a redação existia em
duas ilhas isoladas -- `src/utils/error_interceptor.py` (CHATR-113) e
`src/utils/http_client.py` (CHATR-176) -- e ambas só sanitizavam o payload
enviado ao interceptor de erros. Nenhuma das duas tocava a saída de `logger.*`,
que é o que a barreira de `src/utils/log.py` passa a fazer com o que está aqui.

Só stdlib e nenhum import de `src/`: este módulo precisa ficar embaixo de
`error_interceptor` e `http_client` na ordem de import, e é carregado antes do
preflight.

## Três caminhos de cobertura, porque um só não dá conta

1. **Formato** (`redigir_padroes_pii`) -- CPF, CNPJ, telefone, e-mail e blob têm
   forma reconhecível e são pegos onde quer que apareçam.
2. **Chave** (`redigir_chaves_sensiveis` e `redigir_estrutura`) -- nome, endereço
   e proprietário não têm forma nenhuma; o que denuncia é o rótulo. Cobre tanto
   dict de pé quanto dict já convertido para string.
3. **Máscara no call site** (`mascarar_nome` e companhia) -- quando o valor
   precisa continuar parcialmente legível.

O que escapa aos três: dado sem formato e sem rótulo, do tipo
`f"Nome coletado: {nome}"` ou o texto livre que o cidadão escreveu. Esse é
corrigido apagando a linha de log, não redigindo-a.

## O que deliberadamente NÃO é redigido

Inscrição imobiliária, protocolo, timestamp, `trace_id` e bairro. Nenhum
identifica uma pessoa sozinho, e são o que resta para diagnosticar um
atendimento depois que o resto foi redigido. Os padrões numéricos abaixo são
estritos justamente para não engolir esses valores: `1756213800000` (epoch em
ms), `1790000000` (epoch em s) e `12345678` (inscrição) atravessam intactos.
"""

import re
from typing import Any, Iterable, Optional, Set


# ---------------------------------------------------------------------------
# Marcadores
# ---------------------------------------------------------------------------
#
# Nenhum marcador casa com nenhum dos padrões abaixo, então aplicar a redação
# duas vezes sobre o mesmo texto é inofensivo -- o que importa porque a barreira
# de log passa duas vezes (patcher e sink).

MARCADOR_GENERICO = "[REDACTED]"
MARCADOR_CREDENCIAL = "<redacted>"


# ---------------------------------------------------------------------------
# Chaves sensíveis
# ---------------------------------------------------------------------------
#
# A decisão é por **token**, e não por chave inteira, porque os payloads deste
# projeto misturam convenções: `cpf`, `cpf_cnpj`, `cpfCnpj`, `enderecoImovel`,
# `proprietarioPrincipal`, `nomeRequerente`, `logradouro_nome_ipp`, `phones`.
# Uma lista de chaves exatas erraria em quase todas.

# Credencial: comparação exata, usada pelo `redact_body` do `http_client` e pela
# regex de query string. As três últimas são da signed URL do GCS -- ela é uma
# capability, quem tem a URL baixa o arquivo sem autenticar, então redigir a
# assinatura é o que invalida a URL para quem lê o log.
CHAVES_CREDENCIAL: Set[str] = {
    "token",
    "access_token",
    "refresh_token",
    "id_token",
    "api_key",
    "apikey",
    "key",
    "secret",
    "client_secret",
    "password",
    "senha",
    "authorization",
    "chaveacesso",
    "chave_acesso",
    "signature",
    "x-goog-credential",
    # Por extenso mesmo com `signature` na lista: `redigir_credenciais` casa por
    # substring e cobriria os dois, mas o `redact_body` do `http_client` compara
    # a chave inteira, e ali "X-Goog-Signature" não casava com "signature" -- a
    # assinatura saía em claro sempre que chegasse como campo próprio, e não
    # embutida na URL (CHATR-153).
    "x-goog-signature",
    "googleaccessid",
}

# Um token destes em qualquer parte da chave marca o valor como sensível.
TOKENS_SENSIVEIS: Set[str] = {
    # documento
    "cpf",
    "cnpj",
    "cpfcnpj",
    "documento",
    "rg",
    # identidade
    "nome",
    "name",
    "proprietario",
    "contribuinte",
    "requerente",
    "solicitante",
    "cliente",
    # contato
    "email",
    "mail",
    "telefone",
    "telefones",
    "celular",
    "whatsapp",
    "phone",
    "phones",
    # endereço
    "endereco",
    "endereço",
    "logradouro",
    "complemento",
    "cep",
    # conteúdo
    "base64",
    "pdf",
    # credencial
    "token",
    "secret",
    "password",
    "senha",
    "authorization",
    "signature",
    "key",
}

# Um token destes desarma os de cima na mesma chave. Sem isso, `nome_servico`,
# `service_name`, `table_name` e `bairro_nome` seriam redigidos -- e são
# exatamente o que sobra para entender a linha de log.
TOKENS_NAO_SENSIVEIS: Set[str] = {
    "servico",
    "service",
    "tool",
    "table",
    "tabela",
    "field",
    "campo",
    "function",
    "funcao",
    "flow",
    "flowname",
    "event",
    "evento",
    "bairro",
    "cidade",
    "municipio",
    "estado",
    "uf",
}

# Chave sensível cujos tokens, isolados, não denunciam nada.
CHAVES_EXATAS_SENSIVEIS: Set[str] = {
    "userid",
    "customerwhatsappnumber",
    "chaveacesso",
    "xgoogcredential",
    "xgoogsignature",
    "googleaccessid",
}


def _tokens_da_chave(chave: str) -> Set[str]:
    """
    Quebra a chave em tokens, entendendo snake_case, kebab-case e camelCase.

    `cpfCnpj` -> {cpf, cnpj} · `enderecoImovel` -> {endereco, imovel} ·
    `logradouro_nome_ipp` -> {logradouro, nome, ipp}
    """
    partes = re.split(r"[^A-Za-z0-9]+", str(chave))
    tokens = set()
    for parte in partes:
        for token in re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", parte).split():
            tokens.add(token.lower())
    return tokens


def chave_e_sensivel(chave: Any) -> bool:
    """True se o valor associado a esta chave não pode sair em claro."""
    normalizada = re.sub(r"[^a-z0-9]", "", str(chave).lower())
    if normalizada in CHAVES_EXATAS_SENSIVEIS:
        return True
    tokens = _tokens_da_chave(chave)
    if tokens & TOKENS_NAO_SENSIVEIS:
        return False
    return bool(tokens & TOKENS_SENSIVEIS)


def _valor_e_inocuo(valor: Any) -> bool:
    """
    Flag e contador não são PII, mesmo pendurados em chave sensível.

    `email_processed: True`, `cpf_attempts: 2` e `collect_email: False` são o
    rastro que explica o caminho do workflow -- redigi-los é perder o diagnóstico
    sem ganhar privacidade nenhuma. Nenhum dado pessoal cabe em 4 caracteres.
    """
    if valor is None or isinstance(valor, bool):
        return True
    texto = str(valor).strip().strip("'\"")
    if texto.lower() in {"", "none", "true", "false", "null"}:
        return True
    return texto.isdigit() and len(texto) <= 4


# ---------------------------------------------------------------------------
# Padrões
# ---------------------------------------------------------------------------

# Credencial embutida em query string: várias APIs integradas autenticam assim
# (IPTU, entre outras), e exceções do httpx embutem a URL completa na mensagem.
#
# `sorted` na alternação: a ordem de iteração de um set de strings varia entre
# processos (hash randomization), então o padrão compilado mudava a cada boot. O
# resultado da redação é o mesmo, mas um regex estável é o que torna o
# comportamento reproduzível entre um pod e outro.
#
# `re.escape` porque as chaves entram cruas na alternação: hoje só têm `_` e `-`
# e o padrão é o mesmo com ou sem, mas a primeira chave com `.` ou `+` viraria
# curinga em silêncio -- e o silêncio, num controle de redação, é o problema.
#
# `(?<!\w)` ancora o início da chave. Sem ele `key` casava dentro de qualquer
# palavra terminada nela e `monkey=banana` virava `monkey=<redacted>`, comendo
# diagnóstico do relatório de erro. A âncora recusa `\w` mas aceita `-`, que é
# justamente o que mantém `X-Goog-Signature` coberto: `(?<![\w-])` quebraria a
# redação da signed URL do GCS.
_CREDENCIAL_EM_QUERY = re.compile(
    rf"(?<!\w)((?:{'|'.join(re.escape(chave) for chave in sorted(CHAVES_CREDENCIAL))})=)"
    r"[^&\s'\"]+",
    re.IGNORECASE,
)

# Blob longo (base64 ou hex): PDF de guia em base64, assinatura de signed URL,
# JWT. O piso de 64 caracteres contíguos é alto o bastante para não pegar
# palavra de mensagem nem trace_id (32 caracteres).
_BLOB_BASE64 = re.compile(r"(?<![\w+/=])[A-Za-z0-9+/]{64,}={0,2}(?![\w+/=])")

# O lookbehind e os quantificadores possessivos (`++`, Python 3.11+) não são
# capricho: `[\w.+-]+@[\w-]+(?:\.[\w-]+)+` tem backtracking quadrático em texto
# longo sem `@`, e este padrão roda em toda linha de log, sobre texto que o
# cidadão escreveu. Medido em 8 KB de entrada adversarial: 0,30s -> 0,0001s.
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]++@[\w-]++(?:\.[\w-]++)++")

# CPF e CNPJ com pontuação **parcial**. O validador do projeto
# (`divida_ativa/core/models.py:13`) aceita `^\d{3}\.?\d{3}\.?\d{3}-?\d{2}$` --
# cada separador é opcional e independente do outro, então `123.456.78901` e
# `123456789-01` são entradas válidas que chegam ao log. Cobrir só os dois
# extremos (tudo pontuado / tudo colado) deixava o meio de fora.
_CNPJ = re.compile(r"(?<!\d)\d{2}[.\s-]?\d{3}[.\s-]?\d{3}[/\s-]?\d{4}[-\s]?\d{2}(?!\d)")
_CPF = re.compile(r"(?<!\d)\d{3}[.\s-]?\d{3}[.\s-]?\d{3}[-\s]?\d{2}(?!\d)")

# Telefone brasileiro. O rigor aqui é proposital: o padrão antigo do interceptor
# (`\+?\d{10,13}`) engolia qualquer inteiro de 10 a 13 dígitos, incluindo epoch
# em segundos e em milissegundos. Exigir DDD válido (nenhum termina em 0) e
# primeiro dígito do assinante coerente (9 no móvel, 2-5 no fixo) mantém o
# telefone coberto sem apagar timestamp nem identificador.
_TELEFONE_BR = re.compile(
    r"(?<![\d@])"
    r"(?:\+?55[\s.-]?)?"  # DDI opcional
    r"\(?[1-9][1-9]\)?[\s.-]?"  # DDD
    r"(?:9[\s.-]?\d{4}|[2-5]\d{3})"  # móvel (9 + 4 dígitos) ou fixo (4 dígitos)
    r"[\s.-]?\d{4}"
    r"(?!\d)"
)

# Rede de segurança para telefone fora de qualquer formato previsto -- o que
# chega de sistema de terceiro (a API de cadastro do RMI devolve `phones`, e o
# formato é dela) ou digitado pelo cidadão sem DDD. Aqui quem autoriza a redação
# é a palavra ao lado, não o formato do número.
_TELEFONE_COM_GATILHO = re.compile(
    r"(?i)\b(tel|telefone|telefones|celular|celulares|whats|whatsapp|zap|fone|contato)\b"
    r"(\W{0,10})"
    r"(\+?\d[\d\s.\-()]{6,15}\d)"
)


def _regex_de_chaves(tokens: Iterable[str]) -> re.Pattern:
    """
    Monta o padrão `chave: valor` / `chave=valor` para dict já convertido em
    string.

    A chave é reconhecida por token e não por igualdade -- `'enderecoImovel':`
    precisa bater tanto quanto `'endereco':`. Quem decide de fato é
    `chave_e_sensivel`, chamada na substituição: a regex é só o filtro grosso.
    """
    alternativa = "|".join(
        sorted((re.escape(t) for t in tokens), key=len, reverse=True)
    )
    return re.compile(
        r"(?P<abre>['\"]?)"
        rf"(?P<chave>[A-Za-z0-9_]*(?:{alternativa})[A-Za-z0-9_]*)"
        r"(?P<meio>['\"]?\s*[:=]\s*)"
        # O `&` fica de fora do valor para não engolir o resto de uma query
        # string, e o marcador entra como alternativa própria para que um valor
        # já redigido seja reconhecido inteiro em vez de partido no `]`.
        r"(?P<valor>\[REDACTED[^\]]*\]|'[^']*'|\"[^\"]*\"|[^\s,;&}\])]+)",
        re.IGNORECASE,
    )


_CHAVE_VALOR = _regex_de_chaves(TOKENS_SENSIVEIS)


# ---------------------------------------------------------------------------
# Redação de texto
# ---------------------------------------------------------------------------


def redigir_credenciais(texto: Optional[str]) -> Optional[str]:
    """Redige valores de credencial embutidos em URL ou texto livre."""
    if not texto:
        return texto
    return _CREDENCIAL_EM_QUERY.sub(rf"\1{MARCADOR_CREDENCIAL}", texto)


def redigir_padroes_pii(texto: Optional[str]) -> Optional[str]:
    """
    Redige PII que tem formato reconhecível: blob, e-mail, CNPJ, CPF e telefone.

    A ordem importa -- o padrão mais específico primeiro, para que o CPF não
    seja rotulado como telefone (era o caso no interceptor antes do CHATR-167) e
    para que um documento pontuado não seja quebrado ao meio por um padrão de
    dígitos soltos.

    Não é uma limpeza completa: o restante do texto continua legível de
    propósito, porque um log redigido a ponto de não dizer nada não serve para
    diagnosticar.
    """
    if not texto:
        return texto
    redigido = _BLOB_BASE64.sub("[REDACTED-BASE64]", texto)
    redigido = _EMAIL.sub("[REDACTED-EMAIL]", redigido)
    redigido = _CNPJ.sub("[REDACTED-CNPJ]", redigido)
    # Telefone antes de CPF: os dois têm 11 dígitos quando vêm sem pontuação, e
    # o celular com DDD é o mais específico dos dois (exige DDD válido e o 9).
    # Invertido, todo celular sairia rotulado como CPF.
    redigido = _TELEFONE_BR.sub("[REDACTED-PHONE]", redigido)
    redigido = _CPF.sub("[REDACTED-CPF]", redigido)
    redigido = _TELEFONE_COM_GATILHO.sub(r"\1\2[REDACTED-PHONE]", redigido)
    return redigido


def _redigir_valor_da_chave(match: "re.Match") -> str:
    """Redige o valor se a chave for sensível e o valor não for flag/contador."""
    chave, valor = match.group("chave"), match.group("valor")
    if not chave_e_sensivel(chave) or _valor_e_inocuo(valor):
        return match.group(0)
    limpo = valor.strip("'\"")
    if limpo.startswith("[REDACTED") or limpo == MARCADOR_CREDENCIAL:
        return match.group(0)
    return f"{match.group('abre')}{chave}{match.group('meio')}{MARCADOR_GENERICO}"


def redigir_chaves_sensiveis(texto: Optional[str]) -> Optional[str]:
    """
    Redige o valor que vem depois de uma chave sensível.

    É o que cobre nome, endereço e proprietário -- que não têm padrão próprio --
    quando o dado aparece rotulado, tanto em dict já convertido para string
    (`{'nome': 'Fulano'}`) quanto em mensagem escrita à mão (`cpf: 123...`).
    """
    if not texto:
        return texto
    return _CHAVE_VALOR.sub(_redigir_valor_da_chave, texto)


def redigir_texto(texto: Optional[str]) -> Optional[str]:
    """Varredura completa: credencial, padrão de PII e valor de chave sensível."""
    if not texto:
        return texto
    return redigir_chaves_sensiveis(redigir_padroes_pii(redigir_credenciais(texto)))


def redigir_estrutura(obj: Any, *, marcador: str = MARCADOR_GENERICO) -> Any:
    """
    Redige uma estrutura (dict/list) pela chave, recursivamente.

    Preferível à redação por texto sempre que a estrutura ainda existe: olhar a
    chave pega o valor qualquer que seja o formato dele, enquanto a regex só
    pega o que tem forma reconhecível.
    """
    if isinstance(obj, dict):
        return {
            chave: (
                marcador
                if chave_e_sensivel(chave) and not _valor_e_inocuo(valor)
                else redigir_estrutura(valor, marcador=marcador)
            )
            for chave, valor in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [redigir_estrutura(item, marcador=marcador) for item in obj]
    if isinstance(obj, str):
        return redigir_texto(obj)
    return obj


# ---------------------------------------------------------------------------
# Máscaras para uso no call site
# ---------------------------------------------------------------------------
#
# A redação global é a barreira; estas são para quando o log precisa manter uma
# pista do valor (correlacionar atendimento, conferir que o dado certo foi
# coletado). Estavam duplicadas inline em `poda_de_arvore/workflow.py` e em
# `error_interceptor.py`.


def mascarar_ultimos_quatro(valor: str) -> str:
    """
    Mantém visíveis só os últimos 4 caracteres. Um "+" inicial (E.164) é
    preservado, já que não carrega PII por si só.

    Usado incondicionalmente em campo que é sempre telefone (`user_id`,
    `customer_whatsapp_number`), em vez de depender do padrão bater com o
    formato exato -- assim um valor em formato inesperado não vaza por inteiro.

    Examples:
        >>> mascarar_ultimos_quatro("5521999999999")
        '*********9999'
        >>> mascarar_ultimos_quatro("+5521999999999")
        '+*********9999'
    """
    if not valor:
        return valor
    prefixo = "+" if valor.startswith("+") else ""
    resto = valor[len(prefixo) :]
    if len(resto) <= 4:
        return valor
    return f"{prefixo}{'*' * (len(resto) - 4)}{resto[-4:]}"


def mascarar_cpf(cpf: str) -> str:
    """
    `123.456.789-01` -> `XXX.456.789-XX`. Aceita com ou sem pontuação; o que não
    tiver 11 dígitos vira `XXX`, porque mascarar parcialmente um valor de
    formato desconhecido pode deixar mais à mostra do que se imagina.

    Examples:
        >>> mascarar_cpf("12345678901")
        'XXX.456.789-XX'
    """
    if not cpf:
        return "XXX"
    digitos = "".join(c for c in str(cpf) if c.isdigit())
    if len(digitos) != 11:
        return "XXX"
    return f"XXX.{digitos[3:6]}.{digitos[6:9]}-XX"


def mascarar_nome(nome: str) -> str:
    """
    `Fulano de Tal` -> `Fulano T.`. Mantém o primeiro nome, que é o que permite
    conferir com o cidadão sem expor o nome civil completo.
    """
    if not nome:
        return ""
    partes = str(nome).split()
    if not partes:
        return ""
    if len(partes) > 1:
        return f"{partes[0]} {partes[-1][0]}."
    return partes[0]


def mascarar_email(email: str) -> str:
    """`fulano@exemplo.com` -> `fu***@exemplo.com`."""
    if not email:
        return ""
    usuario, arroba, dominio = str(email).partition("@")
    if not arroba:
        return MARCADOR_GENERICO
    return f"{usuario[:2]}***@{dominio}"


def truncar(texto: Optional[str], limite: int = 500) -> Optional[str]:
    """
    Corta o texto no limite e diz quanto foi cortado.

    Truncar não é redigir -- é só o teto de volume para corpo de resposta de
    terceiro, e vem sempre depois de `redigir_texto`, nunca no lugar dela.
    """
    if not texto:
        return texto
    if len(texto) <= limite:
        return texto
    return f"{texto[:limite]}… (+{len(texto) - limite} chars)"
