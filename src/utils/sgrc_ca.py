"""Workaround de cadeia TLS do SGRC.

O servidor do SGRC (`treinapcrj.datametrica.com.br`) NÃO envia o certificado
intermediário `GeoTrust TLS RSA CA G1` na cadeia TLS. Por isso o cliente Python do
`prefeitura_rio.integrations.sgrc` (aiohttp no `async_new_ticket`, requests no `post`)
falha em verificar a conexão: ``SSLCertVerificationError: unable to get local issuer
certificate`` — toda abertura de chamado quebra. Navegadores funcionam porque baixam
o intermediário via AIA; o cliente Python não faz isso por padrão.

Workaround SEGURO (sem desligar a verificação): montamos um CA bundle =
``certifi`` (roots padrão) **+** o intermediário público, e apontamos
``SSL_CERT_FILE`` / ``REQUESTS_CA_BUNDLE`` pra ele. Como é SUPERSET do certifi, NÃO
quebra nenhum outro TLS de saída (Google/Gemini/BigQuery seguem confiando nos mesmos
roots). Validado: com o bundle o handshake verifica (leaf → GeoTrust → DigiCert Global
Root G2, que está em todo trust store); sem ele, ``CERTIFICATE_VERIFY_FAILED``.

Correção de raiz (independe deste código): a Datamétrica configurar o servidor pra
enviar a cadeia completa. Quando isso acontecer, este bundle continua correto (só
deixa de ser necessário). Intermediário público, válido até 2027-11-02.
"""

import os

import certifi

from src.utils.log import logger

# GeoTrust TLS RSA CA G1 — emissor: DigiCert Global Root G2. Obtido via AIA caIssuers
# do leaf (http://cacerts.geotrust.com/GeoTrustTLSRSACAG1.crt). Público e estável.
_GEOTRUST_TLS_RSA_CA_G1_PEM = """\
-----BEGIN CERTIFICATE-----
MIIEjTCCA3WgAwIBAgIQDQd4KhM/xvmlcpbhMf/ReTANBgkqhkiG9w0BAQsFADBh
MQswCQYDVQQGEwJVUzEVMBMGA1UEChMMRGlnaUNlcnQgSW5jMRkwFwYDVQQLExB3
d3cuZGlnaWNlcnQuY29tMSAwHgYDVQQDExdEaWdpQ2VydCBHbG9iYWwgUm9vdCBH
MjAeFw0xNzExMDIxMjIzMzdaFw0yNzExMDIxMjIzMzdaMGAxCzAJBgNVBAYTAlVT
MRUwEwYDVQQKEwxEaWdpQ2VydCBJbmMxGTAXBgNVBAsTEHd3dy5kaWdpY2VydC5j
b20xHzAdBgNVBAMTFkdlb1RydXN0IFRMUyBSU0EgQ0EgRzEwggEiMA0GCSqGSIb3
DQEBAQUAA4IBDwAwggEKAoIBAQC+F+jsvikKy/65LWEx/TMkCDIuWegh1Ngwvm4Q
yISgP7oU5d79eoySG3vOhC3w/3jEMuipoH1fBtp7m0tTpsYbAhch4XA7rfuD6whU
gajeErLVxoiWMPkC/DnUvbgi74BJmdBiuGHQSd7LwsuXpTEGG9fYXcbTVN5SATYq
DfbexbYxTMwVJWoVb6lrBEgM3gBBqiiAiy800xu1Nq07JdCIQkBsNpFtZbIZhsDS
fzlGWP4wEmBQ3O67c+ZXkFr2DcrXBEtHam80Gp2SNhou2U5U7UesDL/xgLK6/0d7
6TnEVMSUVJkZ8VeZr+IUIlvoLrtjLbqugb0T3OYXW+CQU0kBAgMBAAGjggFAMIIB
PDAdBgNVHQ4EFgQUlE/UXYvkpOKmgP792PkA76O+AlcwHwYDVR0jBBgwFoAUTiJU
IBiV5uNu5g/6+rkS7QYXjzkwDgYDVR0PAQH/BAQDAgGGMB0GA1UdJQQWMBQGCCsG
AQUFBwMBBggrBgEFBQcDAjASBgNVHRMBAf8ECDAGAQH/AgEAMDQGCCsGAQUFBwEB
BCgwJjAkBggrBgEFBQcwAYYYaHR0cDovL29jc3AuZGlnaWNlcnQuY29tMEIGA1Ud
HwQ7MDkwN6A1oDOGMWh0dHA6Ly9jcmwzLmRpZ2ljZXJ0LmNvbS9EaWdpQ2VydEds
b2JhbFJvb3RHMi5jcmwwPQYDVR0gBDYwNDAyBgRVHSAAMCowKAYIKwYBBQUHAgEW
HGh0dHBzOi8vd3d3LmRpZ2ljZXJ0LmNvbS9DUFMwDQYJKoZIhvcNAQELBQADggEB
AIIcBDqC6cWpyGUSXAjjAcYwsK4iiGF7KweG97i1RJz1kwZhRoo6orU1JtBYnjzB
c4+/sXmnHJk3mlPyL1xuIAt9sMeC7+vreRIF5wFBC0MCN5sbHwhNN1JzKbifNeP5
ozpZdQFmkCo+neBiKR6HqIA+LMTMCMMuv2khGGuPHmtDze4GmEGZtYLyF8EQpa5Y
jPuV6k2Cr/N3XxFpT3hRpt/3usU/Zb9wfKPtWpoznZ4/44c1p9rzFcZYrWkj3A+7
TNBJE0GmP2fhXhP1D/XVfIW/h0yCJGEiV9Glm/uGOa3DXHlmbAcxSyCRraG+ZBkA
7h4SeM6Y8l/7MBRpPCz6l8Y=
-----END CERTIFICATE-----
"""

_BUNDLE_PATH = "/tmp/sgrc_ca_bundle.pem"
_applied = False


def ensure_sgrc_ca_chain() -> None:
    """Monta o CA bundle (certifi + intermediário) e seta as envs de TLS.

    Idempotente e à prova de falha (nunca derruba o boot). Deve rodar ANTES da
    primeira chamada ao SGRC — o `ssl.create_default_context()` (usado pelo aiohttp)
    e o `requests` leem essas envs no momento da conexão.
    """
    global _applied
    if _applied:
        return
    try:
        with open(certifi.where(), "r", encoding="utf-8") as fh:
            base = fh.read()
        # SEMPRE setamos SSL_CERT_FILE: o `ssl.create_default_context()` (aiohttp)
        # usa o trust store do SO por padrão — NÃO o certifi — a menos que essa env
        # aponte. O intermediário GeoTrust não está no store do SO (intermediários
        # raramente estão), por isso a verificação falha sem este bundle. Montamos
        # certifi (roots completos) + o intermediário (dedup se o certifi já o tiver).
        already = _GEOTRUST_TLS_RSA_CA_G1_PEM.strip() in base
        bundle = base.rstrip() + "\n"
        if not already:
            bundle += _GEOTRUST_TLS_RSA_CA_G1_PEM.strip() + "\n"
        with open(_BUNDLE_PATH, "w", encoding="utf-8") as fh:
            fh.write(bundle)
        os.environ["SSL_CERT_FILE"] = _BUNDLE_PATH
        os.environ["REQUESTS_CA_BUNDLE"] = _BUNDLE_PATH
        _applied = True
        logger.info(
            f"[sgrc_ca] CA bundle (certifi + intermediário GeoTrust"
            f"{' já-presente' if already else ''}) aplicado em {_BUNDLE_PATH} "
            "(SSL_CERT_FILE/REQUESTS_CA_BUNDLE). Workaround p/ a cadeia incompleta do SGRC."
        )
    except Exception as exc:  # noqa: BLE001 — nunca derrubar o boot por causa disso
        logger.warning(f"[sgrc_ca] falhou ao aplicar o CA bundle do SGRC: {exc}")


# Efeito de import: basta `import src.utils.sgrc_ca` (feito no topo do app) pra a
# cadeia TLS ficar pronta antes da primeira conexão ao SGRC. Idempotente (`_applied`),
# então re-imports e a chamada explícita nos testes não re-executam o trabalho.
ensure_sgrc_ca_chain()
