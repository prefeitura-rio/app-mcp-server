"""Testes da blacklist de domínios das fontes da tool `google_search` (CHATR-314).

Cobrem a comparação por domínio ou subdomínio (que substitui a comparação por
trecho da `LINK_BLACKLIST`), a normalização, a leitura do YAML e o arquivo
versionado de verdade: as 135 entradas de produção continuam bloqueadas.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[4]
BLACKLIST_MODULE_PATH = (
    PROJECT_ROOT / "src" / "tools" / "google_search" / "domain_blacklist.py"
)

# Valor da `LINK_BLACKLIST` em produção, staging e no `.env` local em 26/10/2026,
# na mesma ordem. Fixa a migração: nenhuma entrada antiga pode voltar a passar.
LINK_BLACKLIST_DE_PRODUCAO = (
    "rio.rj.gov.br",
    "reddit.com",
    "youtube.com",
    "wikipedia.org",
    "globo.com",
    "g1.globo.com",
    "oglobo.globo.com",
    "uol.com.br",
    "terra.com.br",
    "r7.com",
    "jb.com.br",
    "cnnbrasil.com.br",
    "metropoles.com",
    "estadao.com.br",
    "correiobraziliense.com.br",
    "abril.com.br",
    "extra.globo.com",
    "odia.ig.com.br",
    "meiahora.com.br",
    "diariodorio.com",
    "rj4news.com.br",
    "rionoticias.com.br",
    "jornaldoriodejaneiro.com.br",
    "youtu.be",
    "dailymotion.com",
    "vimeo.com",
    "twitch.tv",
    "rumble.com",
    "instagram.com",
    "facebook.com",
    "linkedin.com",
    "tiktok.com",
    "x.com",
    "bnldata.com.br",
    "infomoney.com.br",
    "revistafatorbrasil.com.br",
    "band.uol.com.br",
    "radio93.com.br",
    "bandnewstv.uol.com.br",
    "sbt.com.br",
    "record.r7.com",
    "redetv.uol.com.br",
    "angra.rj.gov.br",
    "aperibe.rj.gov.br",
    "araruama.rj.gov.br",
    "areal.rj.gov.br",
    "buzios.rj.gov.br",
    "arraial.rj.gov.br",
    "barradopirai.rj.gov.br",
    "barramansa.rj.gov.br",
    "belfordroxo.rj.gov.br",
    "bomjardim.rj.gov.br",
    "bomjesus.rj.gov.br",
    "cabofrio.rj.gov.br",
    "cachoeirasdemacacu.rj.gov.br",
    "cambuci.rj.gov.br",
    "campos.rj.gov.br",
    "cantagalo.rj.gov.br",
    "carapebus.rj.gov.br",
    "cardosomoreira.rj.gov.br",
    "carmo.rj.gov.br",
    "casimirodeabreu.rj.gov.br",
    "levygasparian.rj.gov.br",
    "conceicaodemacabu.rj.gov.br",
    "cordeiro.rj.gov.br",
    "duasbarras.rj.gov.br",
    "duquedecaxias.rj.gov.br",
    "paulodefrontin.rj.gov.br",
    "guapimirim.rj.gov.br",
    "iguaba.rj.gov.br",
    "itaborai.rj.gov.br",
    "itaguai.rj.gov.br",
    "italva.rj.gov.br",
    "itaocara.rj.gov.br",
    "itaperuna.rj.gov.br",
    "itatiaia.rj.gov.br",
    "japeri.rj.gov.br",
    "lajedomuriae.rj.gov.br",
    "macae.rj.gov.br",
    "macuco.rj.gov.br",
    "mage.rj.gov.br",
    "mangaratiba.rj.gov.br",
    "marica.rj.gov.br",
    "mendes.rj.gov.br",
    "mesquita.rj.gov.br",
    "miguelpereira.rj.gov.br",
    "miracema.rj.gov.br",
    "natividade.rj.gov.br",
    "nilopolis.rj.gov.br",
    "niteroi.rj.gov.br",
    "pmnf.rj.gov.br",
    "novaiguacu.rj.gov.br",
    "paracambi.rj.gov.br",
    "paraibadosul.rj.gov.br",
    "paraty.rj.gov.br",
    "patydoalferes.rj.gov.br",
    "petropolis.rj.gov.br",
    "pinheiral.rj.gov.br",
    "pirai.rj.gov.br",
    "porciuncula.rj.gov.br",
    "portoreal.rj.gov.br",
    "quatis.rj.gov.br",
    "queimados.rj.gov.br",
    "quissama.rj.gov.br",
    "resende.rj.gov.br",
    "riobonito.rj.gov.br",
    "rioclaro.rj.gov.br",
    "riodasflores.rj.gov.br",
    "riodasostras.rj.gov.br",
    "santamariamadalena.rj.gov.br",
    "santoantoniodepadua.rj.gov.br",
    "saofidelis.rj.gov.br",
    "pmsfi.rj.gov.br",
    "saogoncalo.rj.gov.br",
    "sjb.rj.gov.br",
    "meriti.rj.gov.br",
    "saojosedeuba.rj.gov.br",
    "sjvriopreto.rj.gov.br",
    "pmspa.rj.gov.br",
    "pmsaojoaodabarra.rj.gov.br",
    "sapucaia.rj.gov.br",
    "saquarema.rj.gov.br",
    "seropedica.rj.gov.br",
    "silvajardim.rj.gov.br",
    "sumidouro.rj.gov.br",
    "tangua.rj.gov.br",
    "teresopolis.rj.gov.br",
    "trajanodemoraes.rj.gov.br",
    "tresrios.rj.gov.br",
    "valenca.rj.gov.br",
    "varresai.rj.gov.br",
    "vassouras.rj.gov.br",
    "voltaredonda.rj.gov.br",
    "rjpet.com.br",
    "prefeiturarj.org",
)

# Cobertas por outra entrada com a comparação por subdomínio: saíram do arquivo.
REDUNDANTES = (
    "g1.globo.com",
    "oglobo.globo.com",
    "extra.globo.com",
    "band.uol.com.br",
    "bandnewstv.uol.com.br",
    "redetv.uol.com.br",
    "record.r7.com",
)

# Candidatos de negócio ainda sem decisão: não entram neste card.
CANDIDATOS_SEM_DECISAO = (
    "querobolsa.com.br",
    "meutudo.com.br",
    "serasa.com.br",
    "rio-de-janeiro.prefeituras.org",
    "riodejaneirobycariocas.com",
    "silvaerinocontabilidade.com.br",
    "calculadorabolsafamilia.com",
)


@pytest.fixture
def db(monkeypatch):
    """Carrega domain_blacklist.py isolado, com logger silencioso."""
    noop = lambda *_a, **_k: None  # noqa: E731
    monkeypatch.setitem(
        sys.modules,
        "src.utils.log",
        types.SimpleNamespace(logger=types.SimpleNamespace(info=noop, warning=noop)),
    )
    spec = importlib.util.spec_from_file_location(
        "test_domain_blacklist_module", BLACKLIST_MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _yaml(tmp_path, conteudo: str) -> Path:
    path = tmp_path / "blacklist.yaml"
    path.write_text(conteudo, encoding="utf-8")
    return path


# --------------------------------------------------------------------------------
# Comparação por domínio ou subdomínio
# --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    ["facebook.com", "m.facebook.com", "www.facebook.com", "a.b.facebook.com"],
)
def test_dominio_e_subdominios_sao_bloqueados(db, host):
    assert db.DomainBlacklist(["facebook.com"]).dominio_bloqueado(host) == (
        "facebook.com"
    )


@pytest.mark.parametrize(
    "host", ["notfacebook.com", "facebook.com.br", "facebook.co", "com", ""]
)
def test_trecho_do_dominio_nao_bloqueia(db, host):
    assert db.DomainBlacklist(["facebook.com"]).dominio_bloqueado(host) is None


@pytest.mark.parametrize("host", ["netflix.com", "fedex.com", "dropbox.com"])
def test_x_com_nao_bloqueia_quem_so_termina_em_x_com(db, host):
    """A `LINK_BLACKLIST` comparava por trecho e barrava estes três."""
    assert db.DomainBlacklist(["x.com"]).dominio_bloqueado(host) is None


def test_x_com_bloqueia_o_proprio_x(db):
    blacklist = db.DomainBlacklist(["x.com"])

    assert blacklist.dominio_bloqueado("x.com") == "x.com"
    assert blacklist.dominio_bloqueado("mobile.x.com") == "x.com"


@pytest.mark.parametrize(
    "host", ["FACEBOOK.COM", "facebook.com.", "WWW.Facebook.Com.", " facebook.com "]
)
def test_caixa_ponto_final_e_www_sao_normalizados(db, host):
    assert db.DomainBlacklist(["facebook.com"]).dominio_bloqueado(host)


def test_entrada_da_lista_tambem_e_normalizada(db):
    blacklist = db.DomainBlacklist(["WWW.Facebook.com."])

    assert blacklist.dominio_bloqueado("m.facebook.com") == "facebook.com"


@pytest.mark.parametrize(
    "url",
    [
        "https://m.facebook.com/story.php?id=1",
        "http://WWW.FACEBOOK.COM:443/perfil",
        "https://usuario:senha@facebook.com/",
        "https://facebook.com./",
    ],
)
def test_url_bloqueada_olha_so_o_host(db, url):
    assert db.DomainBlacklist(["facebook.com"]).url_bloqueada(url) == "facebook.com"


@pytest.mark.parametrize(
    "url",
    [
        "https://prefeitura.rio/?ref=facebook.com",
        "https://prefeitura.rio/facebook.com",
        "https://[invalido/",
        "",
        None,
    ],
)
def test_url_que_nao_e_do_dominio_passa(db, url):
    assert db.DomainBlacklist(["facebook.com"]).url_bloqueada(url) is None


@pytest.mark.parametrize(
    "entrada",
    ["https://facebook.com", "facebook.com/perfil", "facebook", "", "a b.com"],
)
def test_entrada_invalida_falha(db, entrada):
    with pytest.raises(ValueError):
        db.DomainBlacklist([entrada])


def _web(domain=None, title=None):
    return types.SimpleNamespace(domain=domain, title=title, uri="https://x")


@pytest.mark.parametrize(
    "web",
    [
        _web(domain="m.facebook.com"),
        _web(domain="WWW.FACEBOOK.COM.", title="Página qualquer"),
        # O Gemini API não preenche `domain`; o `title` traz o domínio do site.
        _web(title="facebook.com"),
        _web(domain="", title="www.facebook.com"),
    ],
)
def test_fonte_do_grounding_bloqueada_pelo_dominio(db, web):
    assert db.DomainBlacklist(["facebook.com"]).fonte_bloqueada(web) == "facebook.com"


@pytest.mark.parametrize(
    "web",
    [
        _web(domain="prefeitura.rio", title="facebook.com"),
        _web(title="Perfil no facebook.com"),
        _web(title="Facebook"),
        _web(),
        types.SimpleNamespace(uri="https://x"),
    ],
)
def test_fonte_do_grounding_que_passa(db, web):
    """`domain` preenchido manda; título que não tem forma de domínio é ignorado."""
    assert db.DomainBlacklist(["facebook.com"]).fonte_bloqueada(web) is None


# --------------------------------------------------------------------------------
# Leitura do YAML
# --------------------------------------------------------------------------------


@pytest.mark.parametrize("conteudo", ["", "blacklist:\n", "blacklist:\n  a: []\n"])
def test_yaml_vazio_nao_bloqueia_nada(db, tmp_path, conteudo):
    blacklist = db.load_domain_blacklist(_yaml(tmp_path, conteudo))

    assert len(blacklist) == 0
    assert blacklist.url_bloqueada("https://facebook.com/") is None


def test_yaml_junta_as_secoes(db, tmp_path):
    blacklist = db.load_domain_blacklist(
        _yaml(tmp_path, "blacklist:\n  a: [facebook.com]\n  b: [globo.com]\n")
    )

    assert blacklist.dominio_bloqueado("m.facebook.com") == "facebook.com"
    assert blacklist.dominio_bloqueado("g1.globo.com") == "globo.com"


@pytest.mark.parametrize(
    "conteudo",
    [
        "- facebook.com\n",
        "blacklist: [facebook.com]\n",
        "blacklist:\n  redes: facebook.com\n",
        "blacklist:\n  redes: [1, 2]\n",
        "blacklist:\n  redes: [https://facebook.com]\n",
    ],
)
def test_yaml_malformado_falha_no_boot(db, tmp_path, conteudo):
    with pytest.raises(ValueError):
        db.load_domain_blacklist(_yaml(tmp_path, conteudo))


# --------------------------------------------------------------------------------
# Arquivo versionado
# --------------------------------------------------------------------------------


@pytest.fixture
def lista_real(db):
    return db.load_domain_blacklist(db.CONFIG_PATH)


def test_arquivo_versionado_carrega(db, lista_real):
    assert len(LINK_BLACKLIST_DE_PRODUCAO) == 135
    assert len(lista_real) == 135 - len(REDUNDANTES)
    assert db.domain_blacklist.url_bloqueada("https://m.facebook.com/x")


@pytest.mark.parametrize("dominio", LINK_BLACKLIST_DE_PRODUCAO)
def test_toda_entrada_da_lista_antiga_continua_bloqueada(lista_real, dominio):
    assert lista_real.dominio_bloqueado(dominio)


@pytest.mark.parametrize("dominio", REDUNDANTES)
def test_redundantes_sairam_do_arquivo_e_seguem_bloqueadas(lista_real, dominio):
    assert dominio not in lista_real
    assert lista_real.dominio_bloqueado(dominio) not in (None, dominio)


def test_arquivo_nao_tem_entrada_coberta_por_outra(db):
    """Entrada que é subdomínio de outra é redundante: revisar antes de incluir."""
    dominios = sorted(db.load_domain_blacklist(db.CONFIG_PATH)._dominios)
    redundantes = [d for d in dominios for outro in dominios if d.endswith(f".{outro}")]
    assert redundantes == []


@pytest.mark.parametrize("dominio", CANDIDATOS_SEM_DECISAO)
def test_candidatos_de_negocio_nao_entraram(lista_real, dominio):
    assert lista_real.dominio_bloqueado(dominio) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://prefeitura.rio/servicos",
        "https://carioca.rio/servicos/iptu",
        "https://www.rj.gov.br/",
        "https://www.netflix.com/",
        "https://jae.com.br/onde-estamos/",
    ],
)
def test_fontes_comuns_passam(lista_real, url):
    assert lista_real.url_bloqueada(url) is None
