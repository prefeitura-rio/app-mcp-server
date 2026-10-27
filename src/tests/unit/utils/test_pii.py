"""
Testes da redação de PII e credenciais (CHATR-167).

O que este arquivo protege, além do óbvio "o CPF sai redigido":

- **O que NÃO pode ser redigido.** Inscrição imobiliária, protocolo e timestamp
  são o que resta para diagnosticar um atendimento depois que o resto foi
  redigido. O padrão antigo do interceptor (`\\+?\\d{10,13}`) engolia epoch em
  segundos e em milissegundos; metade dos testes numéricos aqui existe para
  garantir que os padrões estritos não voltem atrás nisso.
- **O rótulo certo.** CPF sem pontuação era rotulado `[REDACTED-PHONE]` --
  observação registrada no próprio CHATR-167.
- **Idempotência.** A barreira de log passa duas vezes (patcher e sink), então
  redigir texto já redigido não pode corromper nada.
"""

import time

import pytest

from src.utils.pii import (
    CHAVES_CREDENCIAL,
    MARCADORES_QUERY,
    CHAVES_EXATAS_SENSIVEIS,
    TOKENS_NAO_SENSIVEIS,
    TOKENS_SENSIVEIS,
    TOKENS_SENSIVEIS_GENERICOS,
    chave_e_sensivel,
    mascarar_cpf,
    mascarar_email,
    mascarar_nome,
    mascarar_ultimos_quatro,
    redigir_chaves_sensiveis,
    redigir_credenciais,
    redigir_estrutura,
    redigir_padroes_pii,
    redigir_query,
    redigir_texto,
    truncar,
)


# ---------------------------------------------------------------------------
# Padrões de PII
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "valor,marcador",
    [
        ("123.456.789-01", "[REDACTED-CPF]"),
        ("12345678901", "[REDACTED-CPF]"),
        ("12.345.678/0001-95", "[REDACTED-CNPJ]"),
        ("12345678000195", "[REDACTED-CNPJ]"),
        ("5521999998888", "[REDACTED-PHONE]"),
        ("+5521999998888", "[REDACTED-PHONE]"),
        ("21999998888", "[REDACTED-PHONE]"),
        ("2122334455", "[REDACTED-PHONE]"),
        ("(21) 99999-8888", "[REDACTED-PHONE]"),
        ("fulano.tal@exemplo.com.br", "[REDACTED-EMAIL]"),
        ("QUJDRA" * 12, "[REDACTED-BASE64]"),
    ],
)
def test_redige_pii_com_formato_reconhecivel(valor, marcador):
    redigido = redigir_padroes_pii(f"antes {valor} depois")

    assert valor not in redigido
    assert marcador in redigido
    # O contexto ao redor continua legível: log redigido a ponto de não dizer
    # nada não serve para diagnosticar.
    assert redigido.startswith("antes ") and redigido.endswith(" depois")


def test_cpf_sem_pontuacao_nao_e_rotulado_como_telefone():
    """
    O padrão antigo (`\\+?\\d{10,13}`) pegava o CPF de 11 dígitos e marcava como
    telefone. Redigia, mas mentia sobre o que tinha ali.
    """
    assert redigir_padroes_pii("cpf 12345678901") == "cpf [REDACTED-CPF]"


@pytest.mark.parametrize(
    "preservado",
    [
        "1756213800000",  # epoch em milissegundos
        "1790000000",  # epoch em segundos
        "12345678",  # inscrição imobiliária
        "202612345",  # protocolo
        "4bf92f3577b34da6a3ce929d0e0e4736",  # trace_id do OTel
    ],
)
def test_nao_redige_identificador_nem_timestamp(preservado):
    """
    Sem isso, o log fica redigido demais para servir: é justamente o que sobra
    para amarrar uma linha de log a um atendimento.
    """
    assert preservado in redigir_texto(f"valor {preservado} fim")


def test_preserva_texto_sem_pii():
    assert redigir_texto("falha de conexão com a PGM") == "falha de conexão com a PGM"
    assert redigir_texto("") == ""
    assert redigir_texto(None) is None


# ---------------------------------------------------------------------------
# Redação por chave -- o que não tem padrão próprio
# ---------------------------------------------------------------------------


def test_redige_valor_de_chave_sensivel_em_dict_stringificado():
    """
    `logger.error({"event": ..., "parameters": parameters})` chega ao sink já
    convertido para string. Nome e endereço não têm padrão em texto livre: a
    cobertura vem de olhar a chave.
    """
    redigido = redigir_texto(
        "{'event': 'da_emitir_guia_started', 'parameters': "
        "{'cpf': '12345678901', 'nome': 'Fulano de Tal'}, 'tipo': 'a_vista'}"
    )

    assert "Fulano de Tal" not in redigido
    assert "12345678901" not in redigido
    # O que identifica a linha continua lá.
    assert "da_emitir_guia_started" in redigido
    assert "a_vista" in redigido


def test_redige_chave_sensivel_escrita_a_mao():
    assert "12345678901" not in redigir_texto("[STATE.DATA] cpf: 12345678901")


def test_bairro_continua_legivel():
    """
    Bairro é grosso demais para identificar alguém e é o que permite
    diagnosticar a geolocalização da poda de árvore.
    """
    redigido = redigir_texto('{"endereco": "RUA X, 123", "bairro": "Copacabana"}')

    assert "Copacabana" in redigido
    assert "RUA X" not in redigido


def test_nao_engole_o_resto_da_query_string():
    """
    O valor de uma chave termina no `&` -- do contrário a redação de `token=`
    levaria junto todo o resto da URL, inclusive o que serve para diagnosticar.
    """
    redigido = redigir_texto("https://ex.com/a?token=SEGREDO&inscricao=12345678")

    assert "SEGREDO" not in redigido
    assert "inscricao=12345678" in redigido


@pytest.mark.parametrize(
    "texto",
    [
        "cpf 123.456.789-01 e tel 5521999998888",
        "{'nome': 'Fulano', 'cpf': '12345678901'}",
        "https://ex.com/a?token=SEGREDO&cpf=12345678901",
    ],
)
def test_redacao_e_idempotente(texto):
    """A barreira de log passa duas vezes: patcher e sink."""
    uma = redigir_texto(texto)

    assert redigir_texto(uma) == uma


def test_credencial_em_query_string():
    assert redigir_credenciais("?token=SEGREDO") == "?token=<redacted>"
    assert redigir_credenciais(None) is None


def test_redigir_chaves_sensiveis_ignora_texto_sem_chave():
    assert redigir_chaves_sensiveis("nada a redigir aqui") == "nada a redigir aqui"


def test_parametro_que_termina_em_key_nao_e_credencial():
    """
    A alternação de `_CREDENCIAL_EM_QUERY` casava no fim de qualquer palavra, e
    `sortkey=asc` saía como `<redacted>` -- apagando do log parâmetro de
    paginação e de cache que não é segredo nenhum.
    """
    assert redigir_credenciais("?sortkey=asc&rowkey=42") == "?sortkey=asc&rowkey=42"
    assert redigir_credenciais("cacheKey=abc") == "cacheKey=abc"


def test_assinatura_do_gcs_continua_redigida():
    """
    A fronteira do teste acima não pode incluir o `-`: o que a signed URL manda
    é `X-Goog-Signature=`, e é exatamente casando no fim de uma palavra
    kebab-case que a assinatura é invalidada para quem lê o log.
    """
    url = "https://storage.googleapis.com/b/x.pdf?X-Goog-Signature=SEGREDO&e=1"

    redigido = redigir_credenciais(url)

    assert "SEGREDO" not in redigido
    assert "X-Goog-Signature=<redacted>" in redigido


def test_blob_nao_engole_caminho_nem_rota():
    """
    Um caminho de arquivo é um run longo de `[A-Za-z0-9/]` como o base64, e
    saía como `[REDACTED-BASE64]` -- apagando do log justamente o que localiza o
    erro. O que separa os dois é o formato: pedaços curtos e numerosos entre
    barras contra pedaços longos.
    """
    caminho = "/home/runner/work/appmcpserver/appmcpserver/src/utils/errorinterceptor"
    rota = "/api/v1/consultaImovel/porInscricao/2024/detalhe/imovel/guia/darm/pdf"

    assert redigir_padroes_pii(caminho) == caminho
    assert redigir_padroes_pii(rota) == rota
    # e o blob de verdade continua indo embora, mesmo sem um dígito sequer
    assert redigir_padroes_pii("QUJDRA" * 12) == "[REDACTED-BASE64]"


def test_chave_hifenizada_nao_reintroduz_backtracking_quadratico():
    """
    A classe da chave ganhou o `-` para casar `x-goog-credential` inteira. Se a
    guarda de ancoragem não ganhar junto, o motor reinicia a tentativa depois de
    cada hífen e o custo volta a ser O(n²) -- a mesma falha de antes, por outra
    porta. Medido com a guarda incompleta: 85,66 ms em 4 KB; com ela, 0,12 ms.
    """
    adversarial = "a-" * 16000

    inicio = time.perf_counter()
    redigir_texto(adversarial)
    decorrido = time.perf_counter() - inicio

    assert decorrido < 1.0, f"redação levou {decorrido:.2f}s -- guarda incompleta"


def test_redacao_nao_tem_backtracking_quadratico():
    """
    `_CHAVE_VALOR` roda em toda linha de log, sobre texto que o cidadão
    escreveu, e a barreira o executa duas vezes (patcher e sink). Sem a guarda
    de ancoragem o custo era O(n² · k): 4 KB adversariais custavam 2,4 s de CPU
    **no event loop**, o que é um DoS a partir de uma mensagem de WhatsApp.

    O underscore é o que torna a entrada adversarial: mantém o run inteiro para
    `[A-Za-z0-9_]` e ao mesmo tempo impede que `_BLOB_BASE64` o encurte antes.

    A margem é folgada de propósito -- linear resolve 32 KB em milissegundos e
    quadrático levaria mais de um minuto. Qualquer coisa perto de 1 s significa
    que a guarda saiu do padrão.
    """
    adversarial = "a_" * 16000

    inicio = time.perf_counter()
    redigir_texto(adversarial)
    decorrido = time.perf_counter() - inicio

    assert decorrido < 1.0, f"redação levou {decorrido:.2f}s -- backtracking voltou"


def _custo(entrada: str, repeticoes: int = 3) -> float:
    """
    Melhor tempo de `redigir_texto` sobre `entrada`, em segundos.

    O melhor, e não a média: o que interessa é o custo do algoritmo, e o que uma
    máquina de CI acrescenta por cima -- escalonamento, GC, vizinho barulhento --
    é sempre ruído para cima. Tomar o mínimo tira a maior parte dele.
    """
    melhor = float("inf")
    for _ in range(repeticoes):
        inicio = time.perf_counter()
        redigir_texto(entrada)
        melhor = min(melhor, time.perf_counter() - inicio)
    return melhor


def _razao_de_crescimento(molde: str, base: int = 16_000) -> float:
    """
    Quanto o custo cresce quando a entrada quadruplica.

    Linear devolve ~4, quadrático devolve ~16. É essa razão que o teste afirma,
    e não um tempo absoluto: tempo absoluto depende da máquina e obriga a
    escolher um limite frouxo o bastante para deixar passar justamente a
    regressão que interessa. A razão é adimensional -- uma máquina lenta torna
    os dois lados lentos e a razão não se mexe.
    """
    pequeno = _custo(molde * (base // len(molde)))
    grande = _custo(molde * ((base * 4) // len(molde)))
    return grande / pequeno


@pytest.mark.parametrize(
    "molde",
    [
        pytest.param("a:", id="colon-densa"),
        pytest.param("a=", id="igual-densa"),
        pytest.param("k:v,", id="pares-sem-espaco"),
        pytest.param("a_", id="run-de-underscore"),
        pytest.param("a-", id="run-de-hifen"),
    ],
)
def test_redacao_cresce_linearmente(molde):
    """
    O custo de `redigir_texto` cresce em O(n), e não em O(n²).

    Três vetores já bateram aqui, todos pela mesma porta -- o motor refazendo
    trabalho em cada posição de um run:

    | vetor                    | fechado por | como                        |
    | ---                      | ---         | ---                         |
    | run de `[A-Za-z0-9_]`    | 530fdd6     | guarda de ancoragem         |
    | run de `[A-Za-z0-9_-]`   | 07e32c4     | hífen junto na guarda       |
    | texto denso em `:` / `=` | este        | valor fora do padrão        |

    O terceiro é o que as duas guardas anteriores não alcançavam: elas impedem
    o reinício dentro da **chave**, e o custo estava na varredura do **valor**,
    que ficava num lookahead guloso e sem teto. Medido antes da correção:
    17,01 ms em 4 KB, 259,06 ms em 16 KB e 1.051,01 ms em 32 KB -- quatro vezes
    o custo a cada dobra, no thread do event loop, a partir de uma mensagem de
    WhatsApp.

    O teste afirma o **crescimento**, não o tempo: quadruplicar a entrada tem de
    quadruplicar o custo (~4), e não multiplicá-lo por dezesseis. O limite de 8
    fica no meio dos dois em escala log, longe o bastante de ambos para não
    depender da velocidade da máquina.
    """
    razao = _razao_de_crescimento(molde)

    assert razao < 8.0, (
        f"custo cresceu {razao:.1f}× ao quadruplicar a entrada; "
        f"linear é ~4 e quadrático é ~16 -- o backtracking voltou"
    )


@pytest.mark.parametrize(
    "molde",
    [
        pytest.param("a:", id="colon-densa"),
        pytest.param("a=", id="igual-densa"),
    ],
)
def test_entrada_adversarial_percorre_o_caminho_real(molde):
    """
    A entrada dos testes de custo não sai pelo atalho.

    `redigir_chaves_sensiveis` devolve o texto intacto quando ele não tem `:`
    nem `=`, e um teste de custo escrito sobre entrada assim mede o atalho --
    fica verde com o caminho quadrático de pé. Foi o que aconteceu com
    `"a_"*16000` e `"a-"*16000`: nenhum dos dois tem separador, e nenhum dos
    dois chegava a `_CHAVE_VALOR`.

    A prova é positiva: um CPF plantado no fim da entrada adversarial só sai
    redigido se o laço tiver percorrido tudo até lá.
    """
    entrada = molde * 8000 + " cpf: 12345678901"

    assert ":" in entrada or "=" in entrada
    assert "[REDACTED-CPF]" in redigir_texto(entrada)


# ---------------------------------------------------------------------------
# Valor não citado com mais de uma palavra
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "texto, esperado",
    [
        ("nome: Joao Silva Pereira", "nome: [REDACTED]"),
        ("nome do cliente: Maria da Silva Santos", "nome do cliente: [REDACTED]"),
        ("endereco: Rua Sao Clemente 100 apto 502", "endereco: [REDACTED]"),
        ("proprietario: Jose Carlos de Oliveira", "proprietario: [REDACTED]"),
        ("logradouro=Avenida Rio Branco 156", "logradouro=[REDACTED]"),
    ],
)
def test_redige_valor_nao_citado_ate_o_fim(texto, esperado):
    """
    Valor sem aspas e com mais de uma palavra sai redigido inteiro.

    Enquanto o valor vivia no lookahead, a classe dele excluía `\\s` e o valor
    terminava na primeira palavra: `nome: Joao Silva Pereira` saía como
    `nome: [REDACTED] Silva Pereira`, com o sobrenome em claro. É o formato da
    mensagem interpolada à mão (`f"nome: {nome}"`), que é exatamente onde nome e
    endereço aparecem com mais de uma palavra -- o dict convertido em string vem
    com aspas e nunca teve o problema.
    """
    assert redigir_texto(texto) == esperado


def test_valor_nao_citado_para_no_proximo_par():
    """
    Atravessar o espaço não pode engolir o par seguinte.

    Redigir demais custa diagnóstico, que é o outro lado da mesma moeda: uma
    linha redigida a ponto de não dizer nada não serve para investigar
    atendimento nenhum. O valor termina antes do que parecer uma nova chave.
    """
    assert (
        redigir_texto("cpf: 12345678901 status: ok") == "cpf: [REDACTED-CPF] status: ok"
    )
    assert redigir_texto("nome: Joao Silva, cpf: 123") == "nome: [REDACTED], cpf: 123"


def test_valor_nao_citado_nao_atravessa_a_linha():
    """
    Num traceback a linha seguinte é outro frame, não a continuação do nome.
    """
    assert redigir_texto('nome: Joao Silva\n  File "x.py", line 1') == (
        'nome: [REDACTED]\n  File "x.py", line 1'
    )


def test_valor_citado_respeita_aspas_escapadas():
    texto = '{"nome": "Maria \\"da Silva\\" Santos", "cpf": "12345678901"}'

    assert redigir_texto(texto) == '{"nome": [REDACTED], "cpf": "[REDACTED-CPF]"}'


# ---------------------------------------------------------------------------
# Redação de estrutura
# ---------------------------------------------------------------------------


def test_redige_estrutura_pela_chave_recursivamente():
    limpo = redigir_estrutura(
        {
            "event": "consulta",
            "parameters": {"cpf": "12345678901", "nome": "Fulano"},
            "itens": [{"telefone": "5521999998888"}],
        }
    )

    assert limpo["event"] == "consulta"
    assert limpo["parameters"]["cpf"] == "[REDACTED]"
    assert limpo["parameters"]["nome"] == "[REDACTED]"
    assert limpo["itens"][0]["telefone"] == "[REDACTED]"


def test_redige_estrutura_aplica_padroes_no_que_sobra():
    """
    Chave inocente com valor sensível dentro: a chave não denuncia, o formato
    sim.
    """
    limpo = redigir_estrutura({"observacao": "ligar para 5521999998888"})

    assert "5521999998888" not in limpo["observacao"]


def test_redige_estrutura_alcanca_set_e_bytes():
    """
    Os dois caíam no `return obj` final e passavam intactos -- um telefone em
    `{"21999998888"}` ou em `b"..."` sob chave não-sensível saía por inteiro.
    """
    assert redigir_estrutura({"valores": {"21999998888"}}) == {
        "valores": {"[REDACTED-PHONE]"}
    }
    assert redigir_estrutura(b"cpf 123.456.789-01") == "cpf [REDACTED-CPF]"


def test_redige_estrutura_nao_estoura_com_set_de_elemento_nao_hashable():
    """
    Redigir converte tupla em lista, que não é hashable. Deixar o `TypeError`
    subir aqui suprimiria a linha de log inteira, porque a barreira falha
    fechado -- então o set vira lista em vez de estourar.
    """
    assert redigir_estrutura({"itens": {("21999998888",)}}) == {
        "itens": [["[REDACTED-PHONE]"]]
    }


def test_toda_chave_de_credencial_decide_como_sensivel():
    """
    Terceira porta da mesma classe de bug: `chave_e_sensivel` consultava
    `CHAVES_EXATAS_SENSIVEIS` e os tokens, mas não `CHAVES_CREDENCIAL`. `apikey`
    era redigida na query string e no `redact_body` do `http_client`, e saía em
    claro no dict e no texto do log, porque tokeniza como {apikey} -- que não é
    token sensível nenhum.

    Iterar sobre o conjunto, e não sobre lista escrita à mão, é o que obriga
    chave nova a funcionar nos três caminhos.
    """
    for chave in sorted(CHAVES_CREDENCIAL):
        assert chave_e_sensivel(chave), f"{chave} não decide como sensível"
        assert redigir_estrutura({chave: "SEGREDO"})[chave] != "SEGREDO", (
            f"{chave} sai em claro no dict"
        )
        assert "SEGREDO" not in redigir_texto("{%r: 'SEGREDO'}" % chave), (
            f"{chave} sai em claro no texto"
        )


def test_conjuntos_de_configuracao_sao_imutaveis():
    """
    `http_client.SENSITIVE_KEYS` é um alias para `CHAVES_CREDENCIAL`, não uma
    cópia: um `.add()` em qualquer consumidor mudaria a barreira de log junto.
    """
    for conjunto in (
        CHAVES_CREDENCIAL,
        TOKENS_SENSIVEIS,
        TOKENS_NAO_SENSIVEIS,
        TOKENS_SENSIVEIS_GENERICOS,
        CHAVES_EXATAS_SENSIVEIS,
    ):
        assert isinstance(conjunto, frozenset)


def test_redige_estrutura_preserva_tipos_nao_estruturados():
    assert redigir_estrutura(None) is None
    assert redigir_estrutura(42) == 42
    assert redigir_estrutura(["a", 1]) == ["a", 1]


# ---------------------------------------------------------------------------
# Máscaras de call site
# ---------------------------------------------------------------------------


def test_mascarar_ultimos_quatro():
    assert mascarar_ultimos_quatro("5521999999999") == "*********9999"
    assert mascarar_ultimos_quatro("+5521999999999") == "+*********9999"
    # Nada de útil a mascarar em valor curto -- devolve como está em vez de
    # produzir algo enganoso.
    assert mascarar_ultimos_quatro("1234") == "1234"
    assert mascarar_ultimos_quatro("") == ""


def test_mascarar_cpf():
    assert mascarar_cpf("12345678901") == "XXX.456.789-XX"
    assert mascarar_cpf("123.456.789-01") == "XXX.456.789-XX"
    # Formato desconhecido não é mascarado pela metade: mascarar parcialmente o
    # que não se sabe o que é pode deixar mais à mostra do que se imagina.
    assert mascarar_cpf("123") == "XXX"
    assert mascarar_cpf("") == "XXX"


def test_mascarar_nome():
    assert mascarar_nome("Fulano de Tal") == "Fulano T."
    assert mascarar_nome("Fulano") == "Fulano"
    assert mascarar_nome("") == ""


def test_mascarar_email():
    assert mascarar_email("fulano@exemplo.com") == "fu***@exemplo.com"
    assert mascarar_email("a@exemplo.com") == "a***@exemplo.com"
    assert mascarar_email("sem-arroba") == "[REDACTED]"
    assert mascarar_email("") == ""


def test_truncar_diz_quanto_cortou():
    assert truncar("abc", 10) == "abc"
    assert truncar("a" * 12, 10) == "a" * 10 + "… (+2 chars)"
    assert truncar(None) is None


# ---------------------------------------------------------------------------
# Pontuação parcial -- o que o validador do projeto aceita de fato
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cpf",
    [
        "12345678901",  # como a poda grava (re.sub(r"\D", "", v))
        "123.456.789-01",  # como o cidadão digita
        "123.456.78901",  # máscara parcial: o validador aceita
        "123456789-01",  # idem
        "123 456 789 01",  # separado por espaço
    ],
)
def test_cpf_e_redigido_em_qualquer_pontuacao(cpf):
    """
    `divida_ativa/core/models.py:13` valida com `^\\d{3}\\.?\\d{3}\\.?\\d{3}-?\\d{2}$`:
    cada separador é opcional **e independente**. Cobrir só "tudo pontuado" e
    "tudo colado" deixava o meio de fora -- e o meio é entrada válida.
    """
    redigido = redigir_padroes_pii(f"documento {cpf} do contribuinte")

    assert cpf not in redigido
    assert "[REDACTED-CPF]" in redigido


@pytest.mark.parametrize(
    "cnpj", ["12345678000195", "12.345.678/0001-95", "12.345.678/000195"]
)
def test_cnpj_e_redigido_em_qualquer_pontuacao(cnpj):
    assert cnpj not in redigir_padroes_pii(f"cnpj {cnpj} fim")


def test_celular_de_11_digitos_nao_vira_cpf():
    """
    Celular com DDD e CPF têm 11 dígitos quando vêm colados. O celular é o mais
    específico (DDD válido + o 9), então vem primeiro.
    """
    assert redigir_padroes_pii("21999998888") == "[REDACTED-PHONE]"
    assert redigir_padroes_pii("12345678901") == "[REDACTED-CPF]"


def test_telefone_fora_do_formato_e_redigido_pela_palavra_ao_lado():
    """
    `state.data["phone"]` vem da API de cadastro do RMI -- o formato é dela, não
    nosso, e pode chegar sem DDD. Quando o número não tem forma reconhecível,
    quem autoriza a redação é o rótulo ao lado.
    """
    for texto in [
        "- Telefone: 99999-8888",
        "whatsapp 9999 8888",
        "contato: 3460-1746",
    ]:
        assert "[REDACTED-PHONE]" in redigir_texto(texto), texto


# ---------------------------------------------------------------------------
# Chave por token: as convenções que os payloads deste projeto usam de verdade
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "chave",
    [
        "cpf",
        "cpf_cnpj",
        "cpfCnpj",
        "enderecoImovel",
        "endereco_imovel",
        "proprietarioPrincipal",
        "nomeRequerente",
        "nomeContribuinte",
        "logradouro_nome_ipp",
        "phones",
        "user_id",
        "customer_whatsapp_number",
        "arquivoBase64",
        "ChaveAcesso",
    ],
)
def test_chave_sensivel_reconhecida_em_qualquer_convencao(chave):
    """Nomes colhidos dos payloads reais: snake_case, camelCase e plural."""
    assert chave_e_sensivel(chave)


@pytest.mark.parametrize(
    "chave",
    [
        "nome_servico",  # nome do serviço, não da pessoa
        "service_name",
        "table_name",
        "bairro_nome",
        "bairro_nome_ipp",
        "inscricao_imobiliaria",
        "event",
        "flowname",
    ],
)
def test_chave_de_diagnostico_continua_legivel(chave):
    assert not chave_e_sensivel(chave)


def test_credencial_curta_nao_escapa_pela_isencao_de_valor_inocuo():
    """
    A isenção de valor curto existe para flag e contador, e o argumento dela --
    nenhum dado pessoal cabe em 4 caracteres -- não vale para segredo: PIN, OTP
    e código de acesso moram exatamente nessa faixa.
    """
    assert redigir_estrutura({"senha": "1234"}) == {"senha": "[REDACTED]"}
    assert redigir_estrutura({"token": "9999"}) == {"token": "[REDACTED]"}
    assert redigir_estrutura({"api_key": "0000"}) == {"api_key": "[REDACTED]"}
    assert redigir_texto("senha: 1234") == "senha: [REDACTED]"


def test_veto_de_diagnostico_nao_desarma_token_forte():
    """
    O veto de `TOKENS_NAO_SENSIVEIS` desarmava a chave inteira: bastava um
    `servico` em qualquer posição para `nome_do_cliente_do_servico` sair em
    claro. Ele só pode desarmar token genérico -- `nome` e `name`, que rotulam
    qualquer coisa. `cliente`, `cpf` e `endereco` ganham do veto.
    """
    assert chave_e_sensivel("nome_servico") is False
    assert chave_e_sensivel("service_name") is False
    assert chave_e_sensivel("bairro_nome") is False

    assert chave_e_sensivel("nome_do_cliente_do_servico") is True
    assert chave_e_sensivel("cpf_do_servico") is True
    assert chave_e_sensivel("endereco_do_evento") is True


def test_flag_e_contador_nao_sao_redigidos():
    """
    `email_processed` e `cpf_attempts` carregam token sensível no nome, mas o
    valor é o rastro do caminho do workflow. Nenhum dado pessoal cabe em 4
    caracteres.
    """
    limpo = redigir_estrutura(
        {
            "cpf": "12345678901",
            "email_processed": True,
            "cpf_attempts": 2,
            "collect_email": False,
            "email": None,
        }
    )

    assert limpo["cpf"] == "[REDACTED]"
    assert limpo["email_processed"] is True
    assert limpo["cpf_attempts"] == 2
    assert limpo["collect_email"] is False
    assert limpo["email"] is None


def test_chave_camelcase_em_dict_stringificado():
    redigido = redigir_texto(
        "{'enderecoImovel': 'RUA X 123', 'nome_servico': 'poda_de_arvore'}"
    )

    assert "RUA X 123" not in redigido
    assert "poda_de_arvore" in redigido


# ---------------------------------------------------------------------------
# CHAVES_EXATAS_SENSIVEIS pelo caminho de texto (CHATR-169)
# ---------------------------------------------------------------------------
#
# Regressão encontrada por fuzz: `chave_e_sensivel` reconhecia todas essas
# chaves, mas o padrão de texto era montado só de `TOKENS_SENSIVEIS` e as
# descartava antes de chamar a decisão. O efeito era um vazamento assimétrico --
# redigido em dict de pé, em claro em `str(exception)` e no traceback, que é
# exatamente o que o sink de `log.py` existe para cobrir.


@pytest.mark.parametrize(
    "chave",
    [
        "userId",
        "user_id",
        "chave_acesso",
        "chaveAcesso",
        "x-goog-credential",
        "X-Goog-Signature",
        "GoogleAccessId",
        "customer_whatsapp_number",
    ],
)
def test_chave_exata_sensivel_e_redigida_tambem_em_texto(chave):
    redigido = redigir_texto("{'%s': 'valor-secreto-abc'}" % chave)

    assert "valor-secreto-abc" not in redigido
    assert "[REDACTED]" in redigido


@pytest.mark.parametrize("chave", sorted(CHAVES_EXATAS_SENSIVEIS))
def test_toda_chave_exata_alcanca_os_dois_caminhos(chave):
    """
    O caminho de dict e o caminho de texto não podem divergir.

    Este teste itera sobre o conjunto, e não sobre uma lista escrita à mão, para
    que uma chave nova acrescentada a `CHAVES_EXATAS_SENSIVEIS` seja obrigada a
    funcionar nos dois -- que é o que faltava quando o padrão de texto tinha uma
    fonte de verdade própria.
    """
    assert chave_e_sensivel(chave)
    assert redigir_estrutura({chave: "valor-secreto-abc"})[chave] == "[REDACTED]"
    assert "valor-secreto-abc" not in redigir_texto(
        "{'%s': 'valor-secreto-abc'}" % chave
    )


def test_chave_com_hifen_nao_vem_partida():
    """
    `x-goog-credential` só é sensível inteira: `credential` e `signature`
    isolados não estão em nenhum conjunto. Com a chave partida no hífen, a
    assinatura da signed URL do GCS saía em claro.
    """
    redigido = redigir_texto("{'x-goog-signature': 'abc123assinatura'}")

    assert "abc123assinatura" not in redigido


def test_chave_aninhada_nao_e_engolida_pelo_valor_da_chave_de_fora():
    """
    `parameters` não é sensível, mas o valor dela contém `cpf`. Se o padrão
    consumisse o valor, o scanner retomaria depois dele e o `cpf` nunca seria
    examinado.
    """
    redigido = redigir_texto("{'parameters': {'cpf': '12345678901'}}")

    assert "12345678901" not in redigido


def test_short_circuit_nao_muda_o_resultado():
    """
    Texto sem `:` nem `=` sai antes do motor de chave-valor, e texto sem dígito
    nem `@` sai antes dos padrões de formato. Nenhum dos dois atalhos pode
    mudar a saída -- só o caminho até ela.
    """
    assert redigir_texto("mensagem simples sem par chave valor") == (
        "mensagem simples sem par chave valor"
    )
    assert redigir_texto("cpf 12345678901") == "cpf [REDACTED-CPF]"
    assert redigir_texto("nome: Fulano") == "nome: [REDACTED]"


# ---------------------------------------------------------------------------
# Redação da query de busca (CHATR-314)
# ---------------------------------------------------------------------------
#
# Exemplos reais de outubro de 2026, com os dados trocados. Cada um precisa
# perder o identificador e manter o assunto da busca.


@pytest.mark.parametrize(
    ("query", "esperado", "tipo"),
    [
        (
            "meu cpf é 529.982.247-25 e quero a segunda via do iptu",
            "meu cpf é [cpf] e quero a segunda via do iptu",
            "cpf",
        ),
        ("cpf 52998224725 bolsa família", "cpf [cpf] bolsa família", "cpf"),
        (
            "buraco na Rua Barata Ribeiro, 200, Copacabana",
            "buraco na [endereço], Copacabana",
            "endereco",
        ),
        (
            "moro na rua joão silva 45 em Campo Grande, coleta de lixo",
            "moro na [endereço] em Campo Grande, coleta de lixo",
            "endereco",
        ),
        (
            "Av. Brasil 500 apto 302 - Deodoro poda de árvore",
            "[endereço] - Deodoro poda de árvore",
            "endereco",
        ),
        (
            "Rua Senador Dantas nº 100 Centro, clínica da família",
            "[endereço] Centro, clínica da família",
            "endereco",
        ),
        (
            "meu filho nasceu em 31/01/2016, qual a escola",
            "meu filho nasceu em [data], qual a escola",
            "data_nascimento",
        ),
        (
            "data de nascimento: 05 de março de 2015 vacina",
            "data de nascimento: [data] vacina",
            "data_nascimento",
        ),
        (
            "CEP 22290-140 coleta seletiva",
            "CEP [cep] coleta seletiva",
            "cep",
        ),
        ("cep 22290140 coleta seletiva", "cep [cep] coleta seletiva", "cep"),
        (
            "meu nome é Maria da Silva e quero saber do cadúnico",
            "meu nome é [nome] e quero saber do cadúnico",
            "nome",
        ),
        (
            "me chamo joao pedro, preciso de ajuda com o iptu",
            "me chamo [nome], preciso de ajuda com o iptu",
            "nome",
        ),
        (
            "minha filha Ana Clara do 3º ano precisa de transferência",
            "minha filha [nome] do 3º ano precisa de transferência",
            "nome",
        ),
        (
            "Sou a Joana, gestante, onde faço pré-natal",
            "Sou a [nome], gestante, onde faço pré-natal",
            "nome",
        ),
        (
            "fulano.tal@gmail.com não recebeu o boleto",
            "[e-mail] não recebeu o boleto",
            "email",
        ),
        ("multa da placa ABC-1234", "multa da placa [placa]", "placa"),
        ("recurso de multa placa RIO1A23", "recurso de multa placa [placa]", "placa"),
        (
            "meu telefone 21 99999-8888 não recebe o sms",
            "meu telefone [telefone] não recebe o sms",
            "telefone",
        ),
    ],
)
def test_redigir_query_redige_identificador_e_mantem_o_assunto(query, esperado, tipo):
    assert redigir_query(query) == (esperado, (tipo,))


@pytest.mark.parametrize(
    "query",
    [
        # Protocolo do 1746 e inscrição imobiliária são o assunto da busca
        "consultar protocolo 1746 202612345",
        "inscrição imobiliária 1234567-8 segunda via do iptu",
        "inscrição imobiliária 01234567 certidão",
        # CPF com dígito verificador inválido não é CPF
        "cpf 123.456.789-00 não encontrado",
        "11111111111",
        # Data de prazo é assunto
        "inscrições até 30/10 para a creche",
        "matrícula 2027 começa em 03/11/2026",
        # Bairro localiza a resposta
        "clínica da família em Campo Grande",
        "ônibus da Ilha do Governador",
        # Dado de saúde é o assunto (LGPD: sensível, mas sem ele a busca perde o
        # sentido)
        "transferência de escola para aluno com TEA",
        "meu filho tem TEA e precisa de mediador",
        "MEU FILHO TEM AUTISMO",
        "gravidez de risco onde fazer pré-natal",
        "Sou a Mãe do aluno com deficiência",
        "sou a responsável pela criança",
        # Frase com "rua" e número que não é endereço
        "a rua da minha casa tem 3 buracos",
        "segunda via do IPTU 2026",
        "ligar para 1746",
        # Logradouro seguido de ano, km ou linha: sem número de casa não há dado
        # pessoal, e o lugar é o assunto da busca
        "Avenida Brasil 2026",
        "Rua das Laranjeiras 2026 iptu",
        "Estrada do Galeão km 5",
        "metrô Largo do Machado linha 1",
        "Praça Seca 2026 feira",
        # Sigla em caixa alta depois do gatilho não é nome
        "sou a PCD e preciso do passe livre",
        "minha mãe IPTU",
        # Sem PII
        "como tirar a segunda via do iptu",
    ],
)
def test_redigir_query_nao_redige_o_que_e_assunto(query):
    assert redigir_query(query) == (query, ())


def test_redigir_query_junta_os_tipos_sem_repetir():
    texto, tipos = redigir_query(
        "meu nome é Ana, cpf 529.982.247-25, moro na Rua das Flores, 10, "
        "nasceu em 01/02/1990, tel 21 99999-8888 e 21 98888-7777"
    )

    assert texto == (
        "meu nome é [nome], cpf [cpf], moro na [endereço], "
        "nasceu em [data], tel [telefone] e [telefone]"
    )
    assert tipos == ("cpf", "telefone", "endereco", "data_nascimento", "nome")


@pytest.mark.parametrize(
    "query",
    [
        "meu nome é Maria da Silva, cpf 529.982.247-25",
        "buraco na Rua Barata Ribeiro, 200, Copacabana",
        "meu filho nasceu em 31/01/2016",
        "CEP 22290-140 placa ABC-1234 fulano@gmail.com",
        "minha filha Ana precisa de vaga",
    ],
)
def test_redigir_query_e_idempotente(query):
    uma_vez, tipos = redigir_query(query)
    duas_vezes, tipos_de_novo = redigir_query(uma_vez)

    assert tipos
    assert duas_vezes == uma_vez
    assert tipos_de_novo == ()


@pytest.mark.parametrize("vazio", [None, ""])
def test_redigir_query_vazio(vazio):
    assert redigir_query(vazio) == (vazio, ())


def test_redigir_query_tem_um_marcador_por_tipo():
    """Os tipos vão para o BigQuery; um tipo sem marcador quebraria a redação."""
    texto, tipos = redigir_query("cpf 529.982.247-25")
    assert set(tipos) <= set(MARCADORES_QUERY)
    assert MARCADORES_QUERY["cpf"] in texto


# Endereço: o dígito só é número de casa com marcador ou fora de ano/km/linha


@pytest.mark.parametrize(
    ("query", "esperado"),
    [
        # Com marcador, até um ano é número de casa
        ("Rua Barata Ribeiro, 2026, Copacabana", "[endereço], Copacabana"),
        ("Rua das Laranjeiras nº 2026", "[endereço]"),
        ("Avenida Brasil n. 1950 Deodoro", "[endereço] Deodoro"),
        # Sem marcador, número que não parece ano continua número de casa
        ("rua joão silva 45 em Campo Grande", "[endereço] em Campo Grande"),
        ("Estrada do Galeão 1500", "[endereço]"),
    ],
)
def test_endereco_com_numero_de_casa_continua_redigido(query, esperado):
    assert redigir_query(query) == (esperado, ("endereco",))


# Nome: a sequência inteira depois do gatilho, em qualquer caixa


@pytest.mark.parametrize(
    ("query", "esperado"),
    [
        # Sem limite de palavras: o sobrenome não fica em claro
        (
            "me chamo Ana Clara dos Santos Oliveira Pereira Lima",
            "me chamo [nome]",
        ),
        (
            "meu nome é Ana Clara dos Santos Oliveira Pereira Lima e quero o iptu",
            "meu nome é [nome] e quero o iptu",
        ),
        # Caixa alta depois de "sou a" ou "meu filho" é nome
        (
            "Sou a MARIA, meu filho PEDRO tem TEA",
            "Sou a [nome], meu filho [nome] tem TEA",
        ),
        (
            "meu filho JOÃO PEDRO DA SILVA estuda na escola",
            "meu filho [nome] estuda na escola",
        ),
        # Nome curto continua redigido; a sigla depois dele não é engolida
        ("sou a Maria e tenho TEA", "sou a [nome] e tenho TEA"),
        ("meu filho Pedro tem TEA", "meu filho [nome] tem TEA"),
        ("meu nome é Ana Clara, cpf 529.982.247-25", "meu nome é [nome], cpf [cpf]"),
        # Palavra de frase encerra o nome em qualquer caixa
        (
            "meu nome é ana silva onde fica a clinica",
            "meu nome é [nome] onde fica a clinica",
        ),
    ],
)
def test_nome_e_redigido_inteiro_em_qualquer_caixa(query, esperado):
    texto, tipos = redigir_query(query)
    assert texto == esperado
    assert "nome" in tipos


def test_pii_e_filtro_normalizam_do_mesmo_jeito():
    """`_NAO_E_NOME` e `_NAO_E_NOME_DE_RUA` são casadas pela mesma `normalizar`
    que o filtro de query usa nas listas dele (zero-width space some, "°" vale
    "º"), e não por uma cópia com regras diferentes."""
    from src.tools.google_search import query_filter
    from src.utils import pii, texto

    assert pii.normalizar is texto.normalizar
    assert query_filter.normalizar is texto.normalizar
    assert not hasattr(pii, "_sem_acento")
