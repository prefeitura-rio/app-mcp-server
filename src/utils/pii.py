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
from functools import lru_cache
from typing import Any, FrozenSet, Optional, Set


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

# Todos os conjuntos daqui são `frozenset`: `http_client.SENSITIVE_KEYS` é um
# alias para `CHAVES_CREDENCIAL`, não uma cópia, então um `.add()` distraído em
# qualquer um dos três módulos consumidores mudaria a barreira inteira.
#
# Credencial: comparação exata, usada pelo `redact_body` do `http_client` e pela
# regex de query string. As três últimas são da signed URL do GCS -- ela é uma
# capability, quem tem a URL baixa o arquivo sem autenticar, então redigir a
# assinatura é o que invalida a URL para quem lê o log.
CHAVES_CREDENCIAL: FrozenSet[str] = frozenset(
    {
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
        # Por extenso mesmo com `signature` na lista: `redigir_credenciais` casa
        # por substring e cobriria os dois, mas o `redact_body` do `http_client`
        # compara a chave inteira, e ali "X-Goog-Signature" não casava com
        # "signature" -- a assinatura saía em claro sempre que chegasse como
        # campo próprio, e não embutida na URL (CHATR-153).
        "x-goog-signature",
        "googleaccessid",
    }
)

# Um token destes em qualquer parte da chave marca o valor como sensível.
TOKENS_SENSIVEIS: FrozenSet[str] = frozenset(
    {
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
)

# Um token destes desarma os de cima na mesma chave. Sem isso, `nome_servico`,
# `service_name`, `table_name` e `bairro_nome` seriam redigidos -- e são
# exatamente o que sobra para entender a linha de log.
TOKENS_NAO_SENSIVEIS: FrozenSet[str] = frozenset(
    {
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
)

# Token sensível que também serve de rótulo genérico para qualquer coisa: é o
# único que o veto acima pode desarmar. `nome_servico` é o nome de um serviço,
# mas `nome_do_cliente_do_servico` continua sendo o nome de uma pessoa -- deixar
# `servico` desarmar a chave inteira era um veto mais forte do que o pretendido.
TOKENS_SENSIVEIS_GENERICOS: FrozenSet[str] = frozenset(
    {
        "nome",
        "name",
    }
)

# Chave sensível cujos tokens, isolados, não denunciam nada.
CHAVES_EXATAS_SENSIVEIS: FrozenSet[str] = frozenset(
    {
        "userid",
        "customerwhatsappnumber",
        "chaveacesso",
        "xgoogcredential",
        "xgoogsignature",
        "googleaccessid",
    }
)


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
    return _chave_e_sensivel(str(chave))


@lru_cache(maxsize=4096)
def _chave_e_sensivel(chave: str) -> bool:
    """
    Decisão por chave, memoizada.

    Roda para toda chave de todo dict de toda linha de log, e o conjunto de
    chaves distintas de um serviço é pequeno e repete sem parar -- 32× medido.
    O cache é keyed em `str(chave)` e não na chave crua de propósito: `1` e
    `True` são iguais para o `lru_cache` e teriam normalizações diferentes.
    """
    normalizada = re.sub(r"[^a-z0-9]", "", chave.lower())
    if normalizada in CHAVES_EXATAS_SENSIVEIS:
        return True
    # Credencial também decide aqui, senão sobra a mesma classe de bug numa
    # terceira porta: `apikey` está em `CHAVES_CREDENCIAL` e é redigida na query
    # string e no `redact_body`, mas tokeniza como {apikey} -- que não é nenhum
    # token sensível -- e saía em claro no dict e no texto do log.
    if normalizada in _CREDENCIAIS_NORMALIZADAS:
        return True
    tokens = _tokens_da_chave(chave)
    sensiveis = tokens & TOKENS_SENSIVEIS
    if not sensiveis:
        return False
    # O veto de diagnóstico só vale enquanto tudo que marcou a chave for
    # genérico. Um token forte (`cpf`, `cliente`, `endereco`) na mesma chave
    # ganha do veto.
    if sensiveis <= TOKENS_SENSIVEIS_GENERICOS and tokens & TOKENS_NAO_SENSIVEIS:
        return False
    return True


# Forma normalizada das chaves de credencial, para comparar com a chave do
# payload sem depender da convenção de escrita (`api_key`, `apiKey`, `API-KEY`).
_CREDENCIAIS_NORMALIZADAS: FrozenSet[str] = frozenset(
    re.sub(r"[^a-z0-9]", "", chave) for chave in CHAVES_CREDENCIAL
)


@lru_cache(maxsize=4096)
def _chave_e_credencial(chave: str) -> bool:
    """
    True se a chave nomeia um segredo, e não um dado pessoal.

    Serve para furar a isenção de `_valor_e_inocuo`: o argumento dela -- nenhum
    dado pessoal cabe em 4 caracteres -- vale para PII e não vale para segredo.
    PIN, OTP e código de acesso moram exatamente nessa faixa, e `senha: 1234`
    estava saindo em claro.
    """
    return re.sub(r"[^a-z0-9]", "", chave.lower()) in _CREDENCIAIS_NORMALIZADAS


def _valor_e_inocuo(valor: Any) -> bool:
    """
    Flag e contador não são PII, mesmo pendurados em chave sensível.

    `email_processed: True`, `cpf_attempts: 2` e `collect_email: False` são o
    rastro que explica o caminho do workflow -- redigi-los é perder o diagnóstico
    sem ganhar privacidade nenhuma. Nenhum dado pessoal cabe em 4 caracteres.

    A recíproca não vale para credencial: ver `_chave_e_credencial`, que desarma
    esta isenção nos dois pontos que a consultam.
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
# `(?<!\w)` ancora o início da chave. Sem ele a alternação casava no fim de
# qualquer palavra terminada nela, e `monkey=banana`, `sortkey=asc`, `rowkey=42`
# e `cacheKey=...` viravam `<redacted>` -- comendo do log e do relatório de erro
# parâmetro de paginação e de cache que não é segredo nenhum. A âncora recusa
# `\w` mas aceita `-`, e é isso que mantém `X-Goog-Signature` coberto:
# `(?<![\w-])` mataria a over-redaction do mesmo jeito, passaria no resto da
# suíte, e desligaria em silêncio a redação de toda signed URL de fornecedor.
_CREDENCIAL_EM_QUERY = re.compile(
    rf"(?<!\w)((?:{'|'.join(re.escape(chave) for chave in sorted(CHAVES_CREDENCIAL))})=)"
    r"[^&\s'\"]+",
    re.IGNORECASE,
)

# Blob longo (base64 ou hex): PDF de guia em base64, assinatura de signed URL,
# JWT. O piso de 64 caracteres contíguos é alto o bastante para não pegar
# palavra de mensagem nem trace_id (32 caracteres).
_BLOB_BASE64 = re.compile(r"(?<![\w+/=])[A-Za-z0-9+/]{64,}={0,2}(?![\w+/=])")


def _e_blob(candidato: str) -> bool:
    """
    Separa blob de caminho e de rota, que também são runs longos de
    `[A-Za-z0-9/]`.

    `/home/runner/work/appmcpserver/appmcpserver/src/utils/errorinterceptor` tem
    70 caracteres e saía como `[REDACTED-BASE64]` -- apagando do log justamente
    o que localiza o erro.

    O alfabeto não separa os dois: `QUJDRA...` é base64 legítimo sem um dígito
    sequer. O que separa é o formato. Em base64 o `/` aparece com probabilidade
    1/64 por caractere, então os pedaços entre barras são longos; em caminho e
    em rota são curtos e numerosos.

    Errar aqui custa um blob não redigido de vez em quando, não um dado pessoal
    em claro -- o conteúdo do blob já está codificado, e a chave que o carrega
    (`pdf`, `base64`) continua sendo redigida por `redigir_chaves_sensiveis`.
    """
    pedacos = candidato.rstrip("=").split("/")
    return len(pedacos) <= 2 or max(len(pedaco) for pedaco in pedacos) >= 32


def _redigir_blob(match: "re.Match") -> str:
    return "[REDACTED-BASE64]" if _e_blob(match.group(0)) else match.group(0)


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


# Padrão `chave: valor` / `chave=valor` para dict já convertido em string.
#
# A chave é **sintática**: qualquer identificador seguido de `:` ou `=`. Quem
# decide se ela é sensível é `chave_e_sensivel`, no callback.
#
# Até o CHATR-167 a alternação dos tokens vivia dentro do padrão, como filtro
# grosso, e isso criava uma segunda fonte de verdade. `chave_e_sensivel`
# consulta `TOKENS_SENSIVEIS`, `CHAVES_EXATAS_SENSIVEIS` e o veto de
# `TOKENS_NAO_SENSIVEIS`; a regex era montada só do primeiro. Chave que a
# decisão reconhece mas cujo token não está em `TOKENS_SENSIVEIS` -- toda a
# `CHAVES_EXATAS_SENSIVEIS` -- nunca chegava ao callback, e o valor saía em
# claro no caminho de texto. `chave_acesso`, `userId`, `GoogleAccessId` e
# `x-goog-credential` eram redigidos em dict de pé e vazavam em `str(exception)`
# e no traceback, que é justamente o que o sink existe para cobrir.
#
# O `-` na classe da chave é o que alcança `x-goog-credential` e
# `X-Goog-Signature`: com `[A-Za-z0-9_]` a chave vinha partida em `credential` e
# `signature`, que não são sensíveis isoladas.
#
# O valor **não entra no padrão**. Ele já esteve num lookahead capturado, e era
# um segundo caminho quadrático, independente do da chave: a alternativa não
# citada (`[^\s,;&}\])]+`) é gulosa e sem teto, então em texto denso em `:` ou
# `=` e sem espaço -- `a:a:a:...` -- o motor varria da posição corrente até o
# fim da string em **cada** posição. A guarda de ancoragem não alcança isso: ela
# impede o reinício dentro de um run de chave, não a varredura do valor.
#
#   | entrada             | com lookahead | sem      |
#   | ---                 | ---           | ---      |
#   | `"a:"*2000`  (4 KB) |     17,01 ms  |  0,52 ms |
#   | `"a:"*8000` (16 KB) |    259,06 ms  |  1,91 ms |
#   | `"a:"*16000`(32 KB) |  1.051,01 ms  |  3,70 ms |
#
# O dobro de entrada custava quatro vezes o tempo -- O(n²) limpo, no thread do
# event loop, a partir de uma mensagem de WhatsApp. É o mesmo vetor do commit
# 530fdd6 pela terceira porta: primeiro o run da chave, depois o hífen, agora o
# valor.
#
# Sem o valor no padrão, o custo por posição passa a ser o da chave mais o
# separador -- limitado --, e quem decide continua sendo `chave_e_sensivel`, que
# é memoizada. A extensão real do valor é varrida em Python por `_fim_do_valor`,
# e **só quando a chave é sensível**: o trabalho caro sai do caminho quente e
# passa a rodar no caminho raro.
#
# A mesma mudança fecha a redação parcial. Preso à classe do lookahead, o valor
# terminava no primeiro espaço, e `nome: Joao Silva Pereira` saía como
# `nome: [REDACTED] Silva Pereira`, com o sobrenome em claro -- o formato de
# mensagem interpolada à mão, que é onde nome e endereço têm mais de uma
# palavra. `_fim_do_valor` atravessa o espaço.
#
# O scanner continua entrando no valor, que é o que faz a chave aninhada ser
# examinada: `{'parameters': {'cpf': '...'}}` casa `parameters`, que não é
# sensível, o `pos` não avança, e o `cpf` de dentro é examinado na sequência.
_FORMA_DA_CHAVE = r"[A-Za-z0-9_](?:[A-Za-z0-9_-]*[A-Za-z0-9_])?"

_CHAVE_VALOR = re.compile(
    # O `-` entra na guarda junto com a classe da chave, e os dois têm de andar
    # juntos: com hífen na chave e fora da guarda, o motor reinicia a tentativa
    # depois de **cada** hífen e o custo volta a ser O(n²) -- 4 KB de `"a-"*2000`
    # medidos em 85,66 ms, contra 0,12 ms com a guarda completa. É a mesma falha
    # que o commit 530fdd6 corrigiu, por outra porta.
    #
    # Não custa cobertura: uma chave hifenizada é casada a partir do primeiro
    # caractere dela (`x-goog-credential` começa no `x`, precedido de aspas ou
    # `{`), nunca do meio.
    r"(?<![A-Za-z0-9_-])"
    r"(?P<abre>['\"]?)"
    rf"(?P<chave>{_FORMA_DA_CHAVE})"
    r"(?P<meio>['\"]?\s*[:=]\s*)",
    re.IGNORECASE,
)

# Fecham o valor onde quer que apareçam, inclusive no meio de uma sequência de
# palavras. O `&` está aqui para não engolir o resto de uma query string, e o
# `\n` para que o valor nunca atravesse a linha: num traceback, a linha seguinte
# é outro frame, não a continuação do nome.
_FIM_DE_VALOR = re.compile(r"[,;&}\])\n\r]")

# Começo de um novo par `chave:`/`chave=`. É o que impede a varredura por
# palavras de engolir o par seguinte da mesma linha: em
# `cpf: 12345678901 status: ok` o valor do `cpf` termina antes de `status`, e o
# `status: ok` continua legível para diagnóstico.
_PROXIMA_CHAVE = re.compile(rf"['\"]?{_FORMA_DA_CHAVE}['\"]?[ \t]*[:=]")


def _fim_do_valor(texto: str, inicio: int) -> int:
    """
    Índice em que termina o valor que começa em `inicio`.

    Roda só para chave sensível, e é isso que mantém o custo total linear: as
    regiões varridas são disjuntas, porque `redigir_chaves_sensiveis` avança o
    `pos` até o fim de todo valor que redige.

    Três formas, na ordem em que aparecem no log:

    1. **Citado** -- `'...'` ou `"..."`: vai até a aspa que fecha. É o caso do
       dict convertido em string, e o mais simples, porque a aspa delimita.
    2. **Já redigido** -- `[REDACTED...]` ou `<redacted>`: reconhecido inteiro,
       para que a segunda passada da barreira (patcher e depois sink) não redija
       o marcador de novo nem o parta ao meio.
    3. **Não citado** -- palavra a palavra, atravessando o espaço. Para no
       delimitador estrutural e antes do próximo par `chave:`.
    """
    n = len(texto)
    if inicio >= n:
        return inicio

    if texto[inicio] in "'\"":
        fecha = texto.find(texto[inicio], inicio + 1)
        return n if fecha == -1 else fecha + 1

    if texto.startswith("[REDACTED", inicio):
        fecha = texto.find("]", inicio)
        return n if fecha == -1 else fecha + 1

    if texto.startswith(MARCADOR_CREDENCIAL, inicio):
        return inicio + len(MARCADOR_CREDENCIAL)

    fim = pos = inicio
    while pos < n:
        if pos > inicio:
            salto = pos
            while salto < n and texto[salto] in " \t":
                salto += 1
            # Sem espaço aqui, a palavra anterior terminou num delimitador.
            if salto == pos or salto >= n:
                break
            if _PROXIMA_CHAVE.match(texto, salto):
                break
            pos = salto
        inicio_palavra = pos
        while (
            pos < n and texto[pos] not in " \t" and not _FIM_DE_VALOR.match(texto, pos)
        ):
            pos += 1
        if pos == inicio_palavra:
            break
        fim = pos
        if pos < n and _FIM_DE_VALOR.match(texto, pos):
            break
    return fim


# ---------------------------------------------------------------------------
# Redação de texto
# ---------------------------------------------------------------------------


# Teste de pertinência barato para `redigir_padroes_pii`. Todo padrão de lá
# exige um dígito ou um `@`, com uma exceção: um blob base64 pode ser só letras.
# Uma varredura em C que falha cedo custa uma fração do preço de rodar os seis
# padrões, e a linha de log escrita à mão quase nunca tem qualquer um dos dois.
_GATILHO_PADROES = re.compile(r"[\d@]|[A-Za-z+/]{64}")


def redigir_credenciais(texto: Optional[str]) -> Optional[str]:
    """
    Redige valores de credencial embutidos em URL ou texto livre.

    O padrão só casa `chave=valor`, então texto sem `=` sai antes do motor.
    """
    if not texto or "=" not in texto:
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
    if not texto or not _GATILHO_PADROES.search(texto):
        return texto
    redigido = _BLOB_BASE64.sub(_redigir_blob, texto)
    redigido = _EMAIL.sub("[REDACTED-EMAIL]", redigido)
    redigido = _CNPJ.sub("[REDACTED-CNPJ]", redigido)
    # Telefone antes de CPF: os dois têm 11 dígitos quando vêm sem pontuação, e
    # o celular com DDD é o mais específico dos dois (exige DDD válido e o 9).
    # Invertido, todo celular sairia rotulado como CPF.
    redigido = _TELEFONE_BR.sub("[REDACTED-PHONE]", redigido)
    redigido = _CPF.sub("[REDACTED-CPF]", redigido)
    redigido = _TELEFONE_COM_GATILHO.sub(r"\1\2[REDACTED-PHONE]", redigido)
    return redigido


def _redigir_valor_da_chave(match: "re.Match", valor: str) -> Optional[str]:
    """
    Substituto para o par chave-valor, ou `None` quando o valor deve ficar.

    Devolver `None` em vez do texto original é o que permite a
    `redigir_chaves_sensiveis` saber que não houve mudança sem ter de comparar
    strings -- e o trecho casado não contém o valor, que o padrão não consome.

    A decisão por chave ficou com quem chama: é ela que dá o direito de varrer o
    valor, e varrer antes de saber que a chave é sensível é o que tornava o
    caminho quadrático.
    """
    chave = match.group("chave")
    if _valor_e_inocuo(valor) and not _chave_e_credencial(str(chave)):
        return None
    limpo = valor.strip("'\"")
    if limpo.startswith("[REDACTED") or limpo == MARCADOR_CREDENCIAL:
        return None
    return f"{match.group('abre')}{chave}{match.group('meio')}{MARCADOR_GENERICO}"


def redigir_chaves_sensiveis(texto: Optional[str]) -> Optional[str]:
    """
    Redige o valor que vem depois de uma chave sensível.

    É o que cobre nome, endereço e proprietário -- que não têm padrão próprio --
    quando o dado aparece rotulado, tanto em dict já convertido para string
    (`{'nome': 'Fulano'}`) quanto em mensagem escrita à mão (`cpf: 123...`).

    A montagem é manual porque o valor não é consumido pelo padrão (ver
    `_CHAVE_VALOR`): `sub` só substituiria a chave e o separador. O `pos` pula
    match que caia dentro de um valor já redigido, que é o preço de deixar o
    scanner entrar no valor -- e é justamente o que faz a chave aninhada ser
    examinada.

    A ordem das três etapas é o que mantém o custo linear, e não é cosmética:
    a chave decide primeiro (memoizada, O(1)), e só uma chave sensível paga a
    varredura do valor. Invertido -- valor primeiro, como fazia o lookahead --,
    toda posição de um texto denso em `:` paga a varredura, e o total é O(n²).

    Texto sem `:` nem `=` não tem par chave-valor nenhum e sai antes de o motor
    ser acionado. É a maioria das linhas de log escritas à mão, e o teste custa
    uma varredura em C contra o custo do padrão inteiro.
    """
    if not texto:
        return texto
    if ":" not in texto and "=" not in texto:
        return texto

    partes: list = []
    pos = 0
    for match in _CHAVE_VALOR.finditer(texto):
        if match.start() < pos:
            continue
        if not chave_e_sensivel(match.group("chave")):
            continue
        fim = _fim_do_valor(texto, match.end())
        substituto = _redigir_valor_da_chave(match, texto[match.end() : fim])
        if substituto is None:
            continue
        partes.append(texto[pos : match.start()])
        partes.append(substituto)
        pos = fim

    if not partes:
        return texto
    partes.append(texto[pos:])
    return "".join(partes)


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
                if chave_e_sensivel(chave)
                and (_chave_e_credencial(str(chave)) or not _valor_e_inocuo(valor))
                else redigir_estrutura(valor, marcador=marcador)
            )
            for chave, valor in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [redigir_estrutura(item, marcador=marcador) for item in obj]
    if isinstance(obj, (set, frozenset)):
        # Set caía no `return obj` e passava intacto: um `{"21999998888"}` sob
        # chave não-sensível saía por inteiro. Volta a ser set quando dá -- um
        # elemento redigido pode deixar de ser hashable (tupla vira lista), e
        # deixar o `TypeError` subir aqui suprimiria a linha toda.
        redigidos = [redigir_estrutura(item, marcador=marcador) for item in obj]
        try:
            return set(redigidos)
        except TypeError:
            return redigidos
    if isinstance(obj, str):
        return redigir_texto(obj)
    if isinstance(obj, (bytes, bytearray)):
        # Idem: bytes passavam intactos. Decodifica só para poder redigir; o
        # que não for texto vira U+FFFD e não custa diagnóstico nenhum, porque
        # byte cru já não era legível no log.
        return redigir_texto(bytes(obj).decode("utf-8", errors="replace"))
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
