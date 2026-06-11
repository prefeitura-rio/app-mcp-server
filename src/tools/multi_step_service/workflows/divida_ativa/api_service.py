from typing import Any

from loguru import logger

from src.tools.divida_ativa import pgm_api


class DividaAtivaAPIService:
    def _payload_consulta(
        self, tipo_consulta: str, valor: str, ano_auto_infracao: str | None = None
    ) -> dict[str, Any]:
        payload = {"origem_solicitação": 0, tipo_consulta: valor}
        if tipo_consulta == "numeroAutoInfracao" and ano_auto_infracao:
            payload["anoAutoInfracao"] = ano_auto_infracao
        return payload

    async def consultar_debitos(
        self, tipo_consulta: str, valor: str, ano_auto_infracao: str | None = None
    ) -> dict[str, Any]:
        parametros_entrada = self._payload_consulta(
            tipo_consulta, valor, ano_auto_infracao
        )
        registros = await pgm_api(
            endpoint="v2/cdas/dividas-contribuinte",
            consumidor="consultar-dividas-contribuinte",
            data=parametros_entrada,
        )

        if "erro" in registros:
            return {
                "api_resposta_sucesso": False,
                "api_descricao_erro": registros["motivos"],
            }

        return self._formatar_consulta(
            tipo_consulta, valor, ano_auto_infracao, registros
        )

    def _formatar_consulta(
        self,
        tipo_consulta: str,
        valor: str,
        ano_auto_infracao: str | None,
        registros: dict[str, Any],
    ) -> dict[str, Any]:
        retorno: dict[str, Any] = {"api_resposta_sucesso": True}
        descricoes = {
            "inscricaoImobiliaria": "Inscrição Imobiliária",
            "cda": "Certidão de Dívida Ativa",
            "cpfCnpj": "CPF/CNPJ",
            "numeroExecucaoFiscal": "Número de Execução Fiscal",
            "numeroAutoInfracao": "Nº e Ano do Auto de Infração",
        }

        msg: list[str] = [f"*{descricoes[tipo_consulta]}*:"]
        msg.append(f"{valor} {ano_auto_infracao}" if ano_auto_infracao else valor)

        if tipo_consulta == "inscricaoImobiliaria":
            msg.append("\n*Endereço do Imóvel:*")
            msg.append(f"{registros.get('enderecoImovel', 'N/A')}")

        debitos_np = registros.get("debitosNaoParceladosComSaldoTotal", {}) or {}
        cdas = debitos_np.get("cdasNaoAjuizadasNaoParceladas", []) or []
        efs = debitos_np.get("efsNaoParceladas", []) or []
        guias = (registros.get("guiasParceladasComSaldoTotal", {}) or {}).get(
            "guiasParceladas", []
        ) or []
        naturezas = registros.get("naturezasDivida", []) or []
        itens_pagamento: dict[int, str] = {}
        debitos_msg: list[dict[str, Any]] = []
        indice = 0

        if naturezas:
            msg.append(f"\n*Natureza da dívida:* {', '.join(naturezas)}")

        if cdas:
            msg.append("\n*Certidões de Dívida Ativa não parceladas:*")
            for cda in cdas:
                indice += 1
                cda_id = str(cda["cdaId"])
                itens_pagamento[indice] = cda_id
                msg.append(f"*{indice}.* *CDA {cda_id}*")
                msg.append(f"Valor: {cda.get('valorSaldoTotal', 'N/A')}")
                debitos_msg.append(
                    {"cda": cda_id, "valor": cda.get("valorSaldoTotal", "N/A")}
                )
            retorno["lista_cdas"] = [str(c["cdaId"]) for c in cdas]

        if efs:
            msg.append("\n*Execuções Fiscais não parceladas:*")
            for ef in efs:
                indice += 1
                numero = str(ef["numeroExecucaoFiscal"])
                itens_pagamento[indice] = numero
                msg.append(f"*{indice}.* *EF {numero}*")
                msg.append(f"Valor: {ef.get('saldoExecucaoFiscalNaoParcelada', 'N/A')}")
                debitos_msg.append(
                    {
                        "ef": numero,
                        "valor": ef.get("saldoExecucaoFiscalNaoParcelada", "N/A"),
                    }
                )
            retorno["lista_efs"] = [str(e["numeroExecucaoFiscal"]) for e in efs]

        if guias:
            msg.append("\n*Guias de parcelamento encontradas:*")
            for guia in guias:
                indice += 1
                numero = str(guia["numero"])
                itens_pagamento[indice] = numero
                msg.append(
                    f"*{indice}.* *Guia nº {numero}* - Data do Último Pagamento: "
                    f"{guia.get('dataUltimoPagamento', 'N/A')}"
                )
                debitos_msg.append(
                    {
                        "guia": numero,
                        "data_ultimo_pagamento": guia.get("dataUltimoPagamento", "N/A"),
                    }
                )
            retorno["lista_guias"] = [str(g["numero"]) for g in guias]

        # Sempre mostra débitos não parcelados (mesmo que seja R$ 0,00)
        msg.append("\n*Débitos não parcelados:*")
        msg.append("Valor total da dívida:")
        msg.append(f"{debitos_np.get('saldoTotalNaoParcelado', 'N/A')}")

        msg.append(f"\n*Data de Vencimento:* {registros.get('dataVencimento', 'N/A')}")

        retorno.update(
            {
                "dicionario_itens": itens_pagamento,
                "total_itens_pagamento": indice,
                "mensagem_divida_contribuinte": "\n".join(msg),
                "guias_quantidade_total": len(retorno.get("lista_guias", [])),
                "efs_cdas_quantidade_total": len(retorno.get("lista_efs", []))
                + len(retorno.get("lista_cdas", [])),
                "total_nao_parcelado": len(efs) + len(cdas),
                "total_parcelado": len(guias),
                "debitos_msg": debitos_msg,
            }
        )
        return retorno

    async def emitir_guia(
        self, dados_consulta: dict[str, Any], itens: list[int], tipo: str
    ):
        entrada = self._preparar_payload_emissao(dados_consulta, itens, tipo)
        if entrada.get("opcao_invalida"):
            return entrada

        endpoint = (
            "v2/guiapagamento/emitir/avista"
            if tipo == "a_vista"
            else "v2/guiapagamento/emitir/regularizacao"
        )
        consumidor = (
            "emitir-guia-vista" if tipo == "a_vista" else "emitir-guia-regularizacao"
        )
        registros = await pgm_api(
            endpoint=endpoint, consumidor=consumidor, data=entrada
        )

        if "erro" in registros:
            return {
                "api_resposta_sucesso": False,
                "api_descricao_erro": registros["motivos"],
            }

        resultado = {**entrada, "api_resposta_sucesso": True}
        for item in registros:
            resultado["codigo_de_barras"] = item.get("codigoDeBarras")
            resultado["link"] = item.get("pdf")
            if item.get("dataVencimento"):
                resultado["data_vencimento"] = item["dataVencimento"]
            if item.get("codigoQrEMVPix"):
                resultado["pix"] = item["codigoQrEMVPix"]

        return resultado

    def _preparar_payload_emissao(
        self, dados_consulta: dict[str, Any], itens: list[int], tipo: str
    ) -> dict[str, Any]:
        dict_itens = dados_consulta.get("dicionario_itens", {}) or {}
        lista_cdas = set(dados_consulta.get("lista_cdas", []) or [])
        lista_efs = set(dados_consulta.get("lista_efs", []) or [])
        lista_guias = set(dados_consulta.get("lista_guias", []) or [])
        cdas: list[str] = []
        efs: list[str] = []
        guias: list[str] = []

        for seq in itens:
            valor = dict_itens.get(seq) or dict_itens.get(str(seq))
            if not valor:
                logger.warning(f"Item de dívida ativa inválido: {seq}")
                return {"opcao_invalida": True}
            if tipo == "a_vista":
                if valor in lista_cdas:
                    cdas.append(valor)
                elif valor in lista_efs:
                    efs.append(valor)
                else:
                    return {"opcao_invalida": True}
            elif valor in lista_guias:
                guias.append(valor)
            else:
                return {"opcao_invalida": True}

        payload: dict[str, Any] = {"origem_solicitação": 0}
        if tipo == "a_vista":
            payload.update({"cdas": cdas, "efs": efs})
        else:
            payload.update({"guias": guias})
        return payload
