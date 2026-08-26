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

import pytest

from src.utils.pii import (
    chave_e_sensivel,
    mascarar_cpf,
    mascarar_email,
    mascarar_nome,
    mascarar_ultimos_quatro,
    redigir_chaves_sensiveis,
    redigir_credenciais,
    redigir_estrutura,
    redigir_padroes_pii,
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
