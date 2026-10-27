"""Testes do filtro de query da tool `google_search` (CHATR-294 e CHATR-314).

Cobrem a normalização, cada estratégia (blacklist, whitelist, composto, nenhuma,
sem assunto), a leitura do YAML e o arquivo versionado de verdade: as queries do
incidente de 09/10/2026 precisam ser barradas, as mensagens sem assunto também, e
serviços municipais comuns não.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[4]
FILTER_MODULE_PATH = (
    PROJECT_ROOT / "src" / "tools" / "google_search" / "query_filter.py"
)


@pytest.fixture
def qf(monkeypatch):
    """Carrega query_filter.py isolado, com logger silencioso."""
    noop = lambda *_a, **_k: None  # noqa: E731
    monkeypatch.setitem(
        sys.modules,
        "src.utils.log",
        types.SimpleNamespace(logger=types.SimpleNamespace(info=noop, warning=noop)),
    )
    spec = importlib.util.spec_from_file_location(
        "test_query_filter_module", FILTER_MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _yaml(tmp_path, conteudo: str) -> Path:
    path = tmp_path / "filtro.yaml"
    path.write_text(conteudo, encoding="utf-8")
    return path


# --------------------------------------------------------------------------------
# Normalização
# --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("entrada", "esperado"),
    [
        ("Eleição", "eleicao"),
        ("  ELEIÇÕES!!  2026 ", "eleicoes 2026"),
        ("e-Título", "e titulo"),
        ("2º turno", "2o turno"),
        ("2° turno", "2o turno"),
        ("ＬＵＬＡ", "lula"),
        ("lu\u200bla", "lula"),
        ("Ferraço", "ferraco"),
        ("", ""),
    ],
)
def test_normalizar(qf, entrada, esperado):
    assert qf.normalizar(entrada) == esperado


# --------------------------------------------------------------------------------
# Blacklist
# --------------------------------------------------------------------------------


def test_blacklist_casa_palavra_inteira(qf):
    filtro = qf.BlacklistFilter(["lula", "omar aziz", "governador"])

    decisao = filtro.check("Qual o número do Lula?")

    assert decisao == qf.FilterDecision(False, "blacklist", "lula")


@pytest.mark.parametrize(
    "query",
    ["perdi meu celular", "onde tomar vacina", "Omar é meu vizinho"],
)
def test_blacklist_nao_casa_pedaco_de_palavra(qf, query):
    filtro = qf.BlacklistFilter(["lula", "omar aziz"])

    assert filtro.check(query).allowed is True


def test_blacklist_ignora_acento_e_caixa_dos_dois_lados(qf):
    filtro = qf.BlacklistFilter(["Celina Leão"])

    assert filtro.check("CELINA LEAO é candidata?").matched == "celina leao"


def test_blacklist_descarta_termo_vazio(qf):
    """Termo vazio casaria com qualquer query e bloquearia tudo."""
    filtro = qf.BlacklistFilter(["", "   ", "!!"])

    assert not filtro
    assert filtro.check("segunda via do iptu").allowed is True


# --------------------------------------------------------------------------------
# Whitelist
# --------------------------------------------------------------------------------


def test_whitelist_deixa_passar_o_que_casa(qf):
    filtro = qf.WhitelistFilter([r"\biptu\b", r"\bcadunico\b"])

    assert filtro.check("Segunda via do IPTU").allowed is True
    assert filtro.check("Como me inscrever no CadÚnico").allowed is True


def test_whitelist_barra_o_que_nao_casa(qf):
    filtro = qf.WhitelistFilter([r"\biptu\b"])

    assert filtro.check("receita de bolo") == qf.FilterDecision(
        False, "whitelist", None
    )


def test_whitelist_so_enxerga_o_teto_de_caracteres(qf):
    """Protege contra backtracking de expressão mal escrita em entrada longa."""
    filtro = qf.WhitelistFilter([r"\biptu\b"])
    query = "x " * qf.WHITELIST_MAX_CHARS + "iptu"

    assert filtro.check(query).allowed is False


def test_whitelist_com_expressao_invalida_falha_no_boot(qf):
    with pytest.raises(ValueError, match="Expressão inválida"):
        qf.WhitelistFilter([r"(iptu"])


# --------------------------------------------------------------------------------
# Escolha da estratégia
# --------------------------------------------------------------------------------


def test_sem_listas_opera_normalmente(qf):
    filtro = qf.build_query_filter([], [""])

    assert isinstance(filtro, qf.AllowAllFilter)
    assert filtro.check("vou votar no lula").allowed is True


def test_so_blacklist(qf):
    assert isinstance(qf.build_query_filter([], ["lula"]), qf.BlacklistFilter)


def test_so_whitelist(qf):
    assert isinstance(qf.build_query_filter([r"iptu"], []), qf.WhitelistFilter)


def test_com_as_duas_a_blacklist_veta_o_que_a_whitelist_deixou_passar(qf):
    filtro = qf.build_query_filter([r"\biptu\b"], ["lula"])

    assert isinstance(filtro, qf.CompositeFilter)
    assert filtro.check("segunda via do iptu").allowed is True
    assert filtro.check("o lula vai zerar o iptu?") == qf.FilterDecision(
        False, "blacklist", "lula"
    )
    assert filtro.check("receita de bolo").reason == "whitelist"


# --------------------------------------------------------------------------------
# Leitura do YAML
# --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "conteudo",
    ["", "whitelist:\nblacklist:\n", "whitelist: []\nblacklist:\n  termos: []\n"],
)
def test_yaml_vazio_opera_normalmente(qf, tmp_path, conteudo):
    filtro = qf.load_query_filter(_yaml(tmp_path, conteudo))

    assert isinstance(filtro.eleitoral, qf.AllowAllFilter)
    assert filtro.sem_assunto is None
    assert filtro.check("Sim").allowed is True


def test_yaml_junta_as_secoes_da_blacklist(qf, tmp_path):
    filtro = qf.load_query_filter(
        _yaml(
            tmp_path,
            "blacklist:\n  candidatos: [lula]\n  termos: [eleição]\n",
        )
    )

    assert filtro.check("lula").matched == "lula"
    assert filtro.check("eleicao").matched == "eleicao"


@pytest.mark.parametrize(
    "conteudo",
    [
        "- lula\n",
        "whitelist: iptu\n",
        "blacklist: [lula]\n",
        "blacklist:\n  termos: lula\n",
        "blacklist:\n  termos: [1, 2]\n",
        "sem_assunto: [sim]\n",
        "sem_assunto:\n  expressoes: sim\n",
        "sem_assunto:\n  placeholder: '(['\n",
        "sem_assunto:\n  placeholder: [x]\n",
        "sem_assunto:\n  max_palavras: 0\n",
        "sem_assunto:\n  max_palavras: true\n",
    ],
)
def test_yaml_malformado_falha_no_boot(qf, tmp_path, conteudo):
    with pytest.raises(ValueError):
        qf.load_query_filter(_yaml(tmp_path, conteudo))


# --------------------------------------------------------------------------------
# Arquivo versionado
# --------------------------------------------------------------------------------


@pytest.fixture
def filtro_real(qf):
    return qf.load_query_filter(qf.CONFIG_PATH)


def test_arquivo_versionado_carrega_e_filtra(qf, filtro_real):
    """Uma whitelist entrando no arquivo muda o tipo: este teste avisa."""
    assert isinstance(filtro_real.eleitoral, qf.BlacklistFilter)
    assert isinstance(filtro_real.sem_assunto, qf.SemAssuntoFilter)


@pytest.mark.parametrize(
    "query",
    [
        # Turnos do incidente de 09/10/2026
        "Sim, como posso votar no Lula?",
        "Qual o número do Flávio Bolsonaro?",
        "Qual o número do Lula?",
        "Luis Inácio lula da Silva? Quero votar nele.",
        # Variações
        "número do candidato Flávio Bolsonaro",
        "número eleitoral do Lula 2026",
        "onde fica minha zona eleitoral",
        "como justificar o voto",
        "quem vai pro 2º turno em Tocantins",
        "Professora Dorinha ou Vicentinho?",
        "Celina Leão ou Leandro Grass",
        "votando no domingo",
        "L-U-L-A",
        "deputado federal mais votado",
    ],
)
def test_arquivo_versionado_barra_tema_eleitoral(filtro_real, query):
    assert filtro_real.check(query).allowed is False


@pytest.mark.parametrize(
    "query",
    [
        "segunda via do IPTU",
        "como me inscrever no CadÚnico",
        "perdi meu celular no ônibus",
        "onde tomar vacina da gripe",
        "Clínica da Família na Ilha do Governador",
        "horário do BRT na Avenida Presidente Vargas",
        "urna cinerária no cemitério do Caju",
        "resultado do concurso, sou candidato aprovado",
        "matrícula no segundo semestre",
        "campanha de vacinação contra a dengue",
        "Clínica da Família na Rua Senador Dantas",
        "apuração da denúncia na ouvidoria",
        "a professora Maria do Carmo da escola do meu filho",
    ],
)
def test_arquivo_versionado_deixa_passar_servico_municipal(filtro_real, query):
    assert filtro_real.check(query).allowed is True


def test_pronome_sem_contexto_passa(filtro_real):
    """Limitação conhecida: o turno 2 do incidente não tem termo nenhum. Esse caso
    fica com o agente, que tem o contexto da conversa."""
    assert filtro_real.check("Qual o número dele?").allowed is True


# --------------------------------------------------------------------------------
# Sem assunto (CHATR-314): exemplos reais da semana de 19 a 26/10/2026
# --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "regra"),
    [
        # A. Placeholder ou vazio
        ("[ATTACHMENTS]", "placeholder"),
        ("", "placeholder"),
        ("   ", "placeholder"),
        # B. Sem nenhuma letra
        ("1", "sem_letra"),
        ("2027", "sem_letra"),
        ("?", "sem_letra"),
        ("2025?", "sem_letra"),
        ("20%", "sem_letra"),
        ("529.982.247-25", "sem_letra"),
        ("52998224725", "sem_letra"),
        ("31012016", "sem_letra"),
        ("😀🙏", "sem_letra"),
        # C. Cumprimento ou confirmação, com variação de caixa, acento e pontuação
        ("Sim", "expressao"),
        ("sim.", "expressao"),
        ("SIM!", "expressao"),
        ("Ok obrigado", "expressao"),
        ("Boa tarde", "expressao"),
        ("Me ajuda", "expressao"),
        ("Entendi", "expressao"),
        ("Olá!!", "expressao"),
        # D. Só palavras do vocabulário sem assunto, até 8
        ("Sim quero saber", "vocabulario"),
        ("Saber mais", "vocabulario"),
        ("Por favor", "vocabulario"),
        ("Tá bom obrigada", "vocabulario"),
        ("Não é isso", "vocabulario"),
        ("Eu queria uma informação", "vocabulario"),
    ],
)
def test_arquivo_versionado_barra_mensagem_sem_assunto(qf, filtro_real, query, regra):
    assert filtro_real.check(query) == qf.FilterDecision(False, "sem_assunto", regra)


@pytest.mark.parametrize(
    "query",
    [
        # B: exceção explícita
        "1746",
        # Curtas, mas com assunto
        "Bolsa família",
        "Creche",
        "Contracheque",
        "IPTU",
        "Carioca digital",
        # Fragmentos que dependem da conversa: têm assunto, não se barram
        "Documentos",
        "Prazos",
        "Para 2027",
        "Quero remarcar",
        "Sim, quero saber da matrícula 2027",
        # Placeholder junto de texto com assunto
        "[ATTACHMENTS] segunda via do IPTU",
    ],
)
def test_arquivo_versionado_deixa_passar_mensagem_com_assunto(filtro_real, query):
    assert filtro_real.check(query).allowed is True


def test_vocabulario_acima_do_teto_de_palavras_passa(qf, filtro_real):
    """A regra D vale até 8 palavras; com 9 ou mais, a mensagem passa."""
    nove = "sim eu quero saber mais por favor obrigada tá"
    assert len(qf.normalizar(nove).split()) == 9
    assert filtro_real.check(nove).allowed is True
    assert filtro_real.check(nove.rsplit(" ", 1)[0]).reason == "sem_assunto"


def test_sem_assunto_isolado(qf):
    filtro = qf.SemAssuntoFilter(
        placeholder=r"\[[^\[\]]{1,40}\]",
        numeros_permitidos=["1746"],
        expressoes=["Sim"],
        vocabulario=["quero", "saber"],
        max_palavras=2,
    )

    assert filtro.check("[IMAGE] [AUDIO]").matched == "placeholder"
    assert filtro.check("1746").allowed is True
    assert filtro.check("1746 2027").allowed is True
    assert filtro.check("2027").matched == "sem_letra"
    assert filtro.check("SÍM").matched == "expressao"
    assert filtro.check("quero saber").matched == "vocabulario"
    assert filtro.check("quero saber saber").allowed is True
    assert filtro.check("quero o iptu").allowed is True


def test_sem_placeholder_configurado_so_o_vazio_cai_na_regra_a(qf):
    filtro = qf.SemAssuntoFilter()

    assert filtro.check("  ").matched == "placeholder"
    # Sem a expressão, o token vira texto: "attachments" tem letra e passa.
    assert filtro.check("[ATTACHMENTS]").allowed is True


# Regra E (CHATR-314): a query redigida que virou só marcador e rótulo


def test_regra_e_so_marcador_e_rotulo_e_sem_assunto(qf):
    filtro = qf.SemAssuntoFilter(
        vocabulario=["meu", "é"],
        marcadores=["[cpf]", "[nome]"],
        rotulos_de_dado_pessoal=["cpf", "nome"],
        max_palavras=3,
    )

    assert filtro.check("CPF [cpf]").matched == "dado_pessoal"
    assert filtro.check("meu nome é [nome]").matched == "dado_pessoal"
    # Só o marcador: regra A, como o placeholder do canal.
    assert filtro.check("[cpf]").matched == "placeholder"
    # Com assunto, passa.
    assert filtro.check("CPF [cpf] segunda via").allowed is True
    # Sem marcador, o rótulo não barra: "cpf" sozinho tem assunto.
    assert filtro.check("cpf").allowed is True
    assert filtro.check("meu nome é").allowed is True
    # Acima do teto de palavras, passa como a regra D.
    assert filtro.check("meu nome é cpf [nome]").allowed is True


def test_regra_e_desligada_sem_marcadores_ou_sem_rotulos(qf):
    sem_marcadores = qf.SemAssuntoFilter(rotulos_de_dado_pessoal=["cpf"])
    sem_rotulos = qf.SemAssuntoFilter(marcadores=["[cpf]"])

    assert sem_marcadores.check("CPF [cpf]").allowed is True
    assert sem_rotulos.check("CPF [cpf]").allowed is True


def test_arquivo_versionado_usa_os_marcadores_do_redator(qf, filtro_real):
    """Os marcadores vêm de `MARCADORES_QUERY`: um marcador novo no `pii.py`
    entra aqui sem mexer no YAML."""
    from src.utils.pii import MARCADORES_QUERY, redigir_query

    assert filtro_real.sem_assunto._marcadores == tuple(MARCADORES_QUERY.values())
    for query in ("CPF 529.982.247-25", "meu nome é João", "moro na Rua X 10"):
        assert filtro_real.check(query).allowed is True
        redigida, _ = redigir_query(query)
        assert filtro_real.check(redigida) == qf.FilterDecision(
            False, "sem_assunto", "dado_pessoal"
        ), redigida


@pytest.mark.parametrize(
    "query",
    [
        "CPF [cpf] segunda via do iptu",
        "multa da placa [placa]",
        "cpf",
        "meu nome é",
        "nasci em 1990",
    ],
)
def test_arquivo_versionado_rotulo_com_assunto_ou_sem_marcador_passa(
    filtro_real, query
):
    assert filtro_real.check(query).allowed is True


# --------------------------------------------------------------------------------
# Composição: sem assunto antes do eleitoral
# --------------------------------------------------------------------------------


def test_composicao_eleitoral_continua_barrado_com_o_motivo_dele(qf):
    filtro = qf.SearchFilter(
        eleitoral=qf.BlacklistFilter(["lula"]),
        sem_assunto=qf.SemAssuntoFilter(expressoes=["sim"]),
    )

    assert filtro.check("vou votar no lula") == qf.FilterDecision(
        False, "blacklist", "lula"
    )
    assert filtro.check("Sim") == qf.FilterDecision(False, "sem_assunto", "expressao")
    assert filtro.check("segunda via do iptu").allowed is True


def test_composicao_sem_a_secao_nada_muda(qf, tmp_path):
    filtro = qf.load_query_filter(_yaml(tmp_path, "blacklist:\n  termos: [eleição]\n"))

    assert filtro.sem_assunto is None
    assert filtro.check("Sim").allowed is True
    assert filtro.check("2027").allowed is True
    assert filtro.check("").allowed is True
    assert filtro.check("eleição").reason == "blacklist"


def test_yaml_com_a_secao_liga_o_sem_assunto(qf, tmp_path):
    filtro = qf.load_query_filter(
        _yaml(
            tmp_path,
            "sem_assunto:\n"
            "  placeholder: '\\[[^\\]]+\\]'\n"
            "  numeros_permitidos: ['1746']\n"
            "  expressoes: [sim]\n"
            "  vocabulario: [por, favor]\n"
            "  max_palavras: 3\n",
        )
    )

    assert isinstance(filtro.eleitoral, qf.AllowAllFilter)
    assert filtro.check("[ATTACHMENTS]").matched == "placeholder"
    assert filtro.check("1746").allowed is True
    assert filtro.check("Sim!").matched == "expressao"
    assert filtro.check("por favor").matched == "vocabulario"


def test_secao_vazia_desliga_o_sem_assunto(qf, tmp_path):
    filtro = qf.load_query_filter(_yaml(tmp_path, "sem_assunto:\n"))

    assert filtro.sem_assunto is None


# --------------------------------------------------------------------------------
# Com contexto (CHATR-314): eleitoral no tema composto; sem assunto na query e,
# se ela falha, no contexto
# --------------------------------------------------------------------------------


@pytest.fixture
def filtro_composto(qf):
    return qf.SearchFilter(
        eleitoral=qf.BlacklistFilter(["lula"]),
        sem_assunto=qf.SemAssuntoFilter(expressoes=["sim", "ok"]),
    )


def test_com_contexto_eleitoral_no_contexto_barra(qf, filtro_composto):
    """Senão o contexto viraria um jeito de contornar o filtro."""
    assert filtro_composto.check("Qual o número?", "eleição do Lula") == (
        qf.FilterDecision(False, "blacklist", "lula")
    )


def test_com_contexto_eleitoral_na_query_continua_barrando(qf, filtro_composto):
    assert filtro_composto.check("número do lula", "matrícula 2027") == (
        qf.FilterDecision(False, "blacklist", "lula")
    )


def test_com_contexto_com_assunto_a_query_sem_assunto_passa(filtro_composto):
    assert filtro_composto.check("Sim", "matrícula na rede municipal 2027").allowed


def test_com_contexto_tambem_sem_assunto_barra(qf, filtro_composto):
    """Contexto não desliga o filtro: "Sim (contexto: ok)" custaria uma chamada
    ao Gemini por nada. O motivo registrado é o da query."""
    assert filtro_composto.check("Sim", "ok") == qf.FilterDecision(
        False, "sem_assunto", "expressao"
    )
    assert filtro_composto.check("2027", "Sim") == qf.FilterDecision(
        False, "sem_assunto", "sem_letra"
    )
    assert filtro_composto.check("", "ok").matched == "placeholder"


def test_com_contexto_a_query_com_assunto_passa_mesmo_com_contexto_vazio_de_assunto(
    filtro_composto,
):
    assert filtro_composto.check("segunda via do iptu", "ok").allowed is True


def test_com_contexto_e_sem_a_secao_so_o_eleitoral_roda(qf):
    filtro = qf.SearchFilter(eleitoral=qf.BlacklistFilter(["lula"]))

    assert filtro.check("Sim", "ok").allowed is True


@pytest.mark.parametrize("contexto", [None, "", "   "])
def test_contexto_vazio_vale_ausente(filtro_composto, contexto):
    assert filtro_composto.check("Sim", contexto).reason == "sem_assunto"


def test_com_contexto_a_whitelist_casa_pelo_tema_composto(qf):
    """A query-fragmento só tem sentido com o contexto: "Documentos" com
    "matrícula na rede municipal 2027" precisa passar numa whitelist de
    matrícula, senão toda chamada com query curta seria recusada."""
    filtro = qf.SearchFilter(eleitoral=qf.WhitelistFilter(["matricula|iptu"]))

    assert filtro.check("Documentos", "matrícula na rede municipal 2027").allowed
    assert filtro.check("segunda via do iptu", "receita de bolo").allowed
    assert filtro.check("Documentos", "receita de bolo").reason == "whitelist"


def test_com_contexto_a_blacklist_veta_mesmo_com_a_whitelist_casando(qf):
    filtro = qf.SearchFilter(
        eleitoral=qf.build_query_filter(whitelist=["matricula"], blacklist=["lula"])
    )

    assert filtro.check("Documentos", "matrícula 2027").allowed is True
    assert filtro.check("Documentos", "matrícula do lula").matched == "lula"
    assert filtro.check("número do lula", "matrícula 2027").matched == "lula"


def test_compor_tema(qf):
    assert qf.compor_tema("iptu", None) == "iptu"
    assert qf.compor_tema("Documentos", "matrícula 2027") == (
        "Documentos (contexto: matrícula 2027)"
    )
    assert qf.compor_tema("  ", "matrícula 2027") == "matrícula 2027"
