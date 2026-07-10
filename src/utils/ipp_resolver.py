"""#D-2 (determinismo do SGRC): re-deriva os códigos IPP (logradouro/bairro) a
partir do endereço AUTORITATIVO, em vez de confiar na cópia do LLM.

O `register_sgrc_ticket` recebia `street_code`/`neighborhood_code` preenchidos pelo
LLM (que deveria copiá-los do `validate_address`). Essa cópia é o elo fraco — a
maior fonte de payload SGRC malformado ("erro ao abrir o chamado"). Aqui re-
derivamos os códigos com o MESMO pipeline do `validate_address`
(`google_geolocator` → `get_endereco_info`), usando o endereço COMPLETO, e só
usamos os do LLM como fallback, pra NÃO regredir quando o serviço de endereço está
fora/lento.

`address_service` é injetado (não importado aqui) pra manter o helper unit-testável
sem tocar rede.
"""

import asyncio

from src.utils.log import logger

# Timeout do lookup best-effort de IPP (#D-2). Bounded pra um serviço de endereço
# travado NÃO segurar a abertura do chamado — cai no fallback (códigos do LLM).
_IPP_LOOKUP_TIMEOUT_SECONDS = 15.0


async def resolve_ipp_codes(
    address_service,
    street: str,
    number: str,
    neighborhood: str,
    zip_code: str,
    llm_street_code: str,
    llm_neighborhood_code: str,
) -> tuple[str, str]:
    """Retorna (street_code, neighborhood_code) re-derivados do endereço completo.

    Só sobrescreve o código do LLM quando a derivação produz um código VÁLIDO (não
    vazio e diferente de "0", que o `get_endereco_info` usa pra "não achei").
    Qualquer falha (geo inválido, timeout, exceção do serviço) mantém o do LLM.
    """
    resolved_street = llm_street_code
    resolved_neigh = llm_neighborhood_code

    # Query COMPLETA (rua + número + bairro + CEP) — a mesma informação que o
    # validate_address teve. Geocodar só "rua, número" perderia bairro/CEP e, em
    # ruas homônimas ou que cruzam bairros, resolveria OUTRO ponto → sobrescreveria
    # códigos corretos com errados. Filtra componentes vazios.
    query = ", ".join(
        part
        for part in [
            street,
            str(number).strip(),
            neighborhood,
            "Rio de Janeiro - RJ",
            zip_code,
        ]
        if part and str(part).strip()
    )

    try:
        async with asyncio.timeout(_IPP_LOOKUP_TIMEOUT_SECONDS):
            geo = await address_service.google_geolocator(query)
            if geo.get("valid"):
                ipp = await address_service.get_endereco_info(
                    latitude=geo["latitude"],
                    longitude=geo["longitude"],
                    logradouro_google=geo.get("logradouro"),
                    bairro_google=geo.get("bairro"),
                )
                derived_street = str(ipp.get("logradouro_id", "") or "")
                derived_neigh = str(ipp.get("bairro_id", "") or "")
                if derived_street and derived_street != "0":
                    resolved_street = derived_street
                if derived_neigh and derived_neigh != "0":
                    resolved_neigh = derived_neigh

                if (
                    resolved_street != llm_street_code
                    or resolved_neigh != llm_neighborhood_code
                ):
                    logger.info(
                        "[resolve_ipp_codes] #D-2 IPP re-derivado do endereço "
                        f"(street_code {llm_street_code!r}→{resolved_street!r}, "
                        f"neighborhood_code {llm_neighborhood_code!r}→{resolved_neigh!r})"
                    )
    except Exception as exc:
        logger.warning(
            "[resolve_ipp_codes] #D-2 re-derivação IPP falhou/expirou — usando os "
            f"códigos do LLM. Erro: {type(exc).__name__}: {exc}"
        )

    return resolved_street, resolved_neigh
