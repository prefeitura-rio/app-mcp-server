"""Ferramenta com orientacoes para alagamentos e enchentes."""

from src.utils.log import logger
from src.utils.tool_versioning import add_tool_version


def _build_flooding_guidelines_text() -> str:
    """Cria texto orientativo unificado e conciso para enchentes e inundacoes."""

    return """Objetivo: orientar o assistente a instruir moradores do Rio a agir com
            seguranca em enchentes e inundacoes.
            Deixe explicito que o tom de voz deve alternar entre acolhimento
            institucional (para tranquilizar e validar sentimentos) e objetividade firme
            quando houver risco iminente.
            Acione estas diretrizes quando o usuario mencionar chuva intensa, agua
            acumulada, vias bloqueadas, necessidade de evacuacao ou duvida preventiva.
            Ajuste o tom conforme a urgencia percebida: utilize voz educativa
            e calma nas orientacoes preventivas (urgencia baixa), apresente sinais
            e preparacao com firmeza moderada em urgencia media, forneca comandos
            acionaveis com contatos visiveis em urgencia alta e empregue frases
            imperativas priorizando vidas quando o risco for critico. Cheque a exatidao
            de dados tecnicos e mantenha todas as respostas alinhadas aos procedimentos
            oficiais da Defesa Civil. Sempre que o quadro for complexo, organize as
            acoes em sequencia clara e apresente alternativas viaveis.
            Abordagem inicial: acolha o usuario com empatia, demonstre presenca
            institucional e verifique de imediato se ha risco para a vida. Se
            identificar perigo urgente, explique com clareza que a prioridade e acionar
            socorros (Defesa Civil 199, Bombeiros 193, SAMU 192, Policia Militar 190) e mantenha tom direto e resolutivo.
            Orientacoes preventivas (antes de chover forte): instrua o cidadao a
            confirmar com a Prefeitura se a casa fica em area de risco, manter calhas,
            ralos e bueiros limpos e acionar o poder publico quando houver entupimentos
            ou tampas danificadas. 
            Reforce a necessidade de descartar lixo em pontos adequados fora de
            areas sujeitas a alagamento, monitorar o nivel de rios ou lagoas proximos
            e planejar rotas alternativas e abrigos seguros (parentes ou estruturas
            oficiais). 
            Instrua o cidadao a guardar documentos e objetos de valor em sacos
            plasticos em locais altos, elevar moveis e desligar aparelhos do disjuntor
            principal. Sugira montar kit com agua potavel, alimentos nao pereciveis,
            roupas, remedios, lanternas e pilhas, evitando velas e lamparinas. Inclua
            orientacao para combinar planos com vizinhos e dar atencao especial a
            idosos, criancas, gestantes e pessoas com mobilidade reduzida.
            Durante a enchente ou inundacao: ajuste o tom para orientar com
            agilidade, mantendo frases curtas e instrucoes sequenciais. Instrua a pessoa
            a fechar portas e janelas, manter distancia da agua contaminada e ajudar
            vulneraveis e animais de estimacao a alcancarem pontos elevados. 
            Explique que nao se deve atravessar ruas, pontes ou avenidas alagadas
            de forma alguma; 
            Recomende usar radio ou aplicativos para identificar rotas seguras,
            ficar atento a buracos e bueiros abertos, abandonar o veiculo se a agua
            subir e buscar locais altos. Oriente a desligar energia no disjuntor,
            fechar o gas se a agua atingir botijao ou tubulacao e acompanhar comunicados
            oficiais. Caso o risco seja extremo, reforce em tom urgente e claro a
            necessidade de seguir imediatamente as instrucoes.
            Apos a agua baixar: retome o tom acolhedor, enfatizando passos de
            cuidado. Oriente o retorno apenas de dia, avaliando rachaduras ou paredes
            estufadas e acione tecnicos se notar risco estrutural. Instrua a remover
            lama e detritos, lavar pisos, paredes, moveis e utensilios com agua
            sanitaria e descartar alimentos, bebidas e remedios que tiveram contato com
            a agua. 
            Reforce o uso de luvas, botas ou sacos plasticos duplos para evitar
            contato com lama, a necessidade de tratar agua de poco ou nascente antes do
            consumo e o cuidado de nao religar energia, gas ou equipamentos eletricos
            umidos sem avaliacao profissional. Alerta sobre sintomas de leptospirose
            transbordamento gradual de rios, mares ou lagos, ou a falha de drenagem que
            atinge areas normalmente secas apos chuvas prolongadas. Encerre reforcando
            que o cidadao deve manter a calma, e seguir informações oficiais da Prefeitura e das equipes de Defesa Civil.
            Checklist final do assistente: revise se a resposta esta tecnicamente
            correta (telefones, procedimentos), se as orientacoes de seguranca
            estao completas e se o tom utilizado corresponde ao nivel de urgencia
            identificado. Garanta clareza, concisao e alinhamento semantico com o
            protocolo da Defesa Civil antes de encerrar e confirme para si que todos os
            criterios de seguranca foram contemplados."""


def flooding_response_guidelines() -> dict:
    """Retorna texto com instrucoes completas sobre resposta a alagamentos."""
    logger.info("Gerando orientacoes textuais para flooding_response_guidelines")
    guidelines_text = _build_flooding_guidelines_text()
    return add_tool_version(guidelines_text)


__all__ = ["flooding_response_guidelines"]
