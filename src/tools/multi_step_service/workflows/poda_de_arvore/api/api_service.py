import json
import math
import re
import httpx
import aiohttp
import pandas as pd
from src.config import env
from loguru import logger
from src.utils.error_interceptor import interceptor
from src.utils.http_client import InterceptedHTTPClient
from async_googlemaps import AsyncClient
from shapely.geometry import Point
from shapely.wkt import loads
import geopandas as gpd
from pathlib import Path
from pydantic import BaseModel
from jellyfish import jaro_similarity
from num2words import num2words




class SGRCAPIService:
    def __init__(self):
        self.api_base_url = "https://api.prefeitura.rio/poda_de_arvore"


    def get_integrations_url(self, endpoint: str) -> str:
        """
        Returns the URL of the endpoint in the integrations service.
        """
        base_url = env.CHATBOT_INTEGRATIONS_URL
        if base_url.endswith("/"):
            base_url = base_url[:-1]
        if endpoint.startswith("/"):
            endpoint = endpoint[1:]
        return f"{base_url}/{endpoint}"


    @interceptor(source={"source": "mcp", "tool": "multi_step_service", "workflow": "poda_de_arvore"})
    async def get_user_info(self, cpf: str) -> dict:
        url = self.get_integrations_url("person")
        key = env.CHATBOT_INTEGRATIONS_KEY
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        }

        try:
            async with InterceptedHTTPClient(
                user_id="unknown",
                source={"source": "mcp", "tool": "multi_step_service", "workflow": "poda_de_arvore", "function": "get_user_info"},
                timeout=30.0
            ) as client:
                response = await client.post(url, headers=headers, content=json.dumps({"cpf": cpf}))
                response.raise_for_status()
                data = response.json()
            return data

        except Exception as exc:  # noqa
            raise Exception(f"Failed to get user info: {exc}") from exc
        

class NearestLocation(BaseModel):
    id_logradouro: int
    name_logradouro: str
    id_bairro: int
    name_bairro: str


class AddressAPIService:
    def __init__(self):
        self.shape_rj = None
        self._load_shape_rj()

    def _load_shape_rj(self):
        """Carrega o shape do Rio de Janeiro uma única vez."""
        try:
            shape_path = Path(__file__).parent.parent.parent / "shape_rj.geojson"
            if shape_path.exists():
                self.shape_rj = gpd.read_file(shape_path).iloc[0]["geometry"]
        except Exception as e:
            logger.warning(f"Não foi possível carregar shape do RJ: {e}")
            self.shape_rj = None

    @interceptor(source={"source": "mcp", "tool": "multi_step_service", "workflow": "poda_de_arvore"})
    async def google_geolocator(self, address: str) -> dict:
        """
        Uses Google Maps API to get the formatted address using geocode
        """

        def set_logradouro(geocode_result: list, accepted_logradouros: dict):
            for _, resultado in enumerate(geocode_result):
                for item in resultado["address_components"]:
                    if any(logradouro in item["types"] for logradouro in accepted_logradouros):
                        logradouro = item["long_name"]
                        resultado = resultado
                        return logradouro, resultado

            logger.info("Google não conseguiu encontrar um logradouro válido")
            return None, None

        def set_additional_info(resultado, accepted_logradouros):
            address_info = {}
            for item in resultado["address_components"]:
                if "street_number" in item["types"]:
                    address_info["numero"] = item["long_name"]
                elif any(logradouro in item["types"] for logradouro in accepted_logradouros):
                    address_info["logradouro"] = item["long_name"]
                elif ("sublocality" in item["types"] or "sublocality_level_1" in item["types"]):
                    address_info["bairro"] = item["long_name"]
                elif "postal_code" in item["types"]:
                    address_info["cep"] = validate_cep(item["long_name"])
                elif "administrative_area_level_2" in item["types"]:
                    address_info["cidade"] = item["long_name"]
                elif "administrative_area_level_1" in item["types"]:
                    address_info["estado"] = item["short_name"]
            
            return address_info

        def validate_cep(cep: str) -> str:
            cep_formatado = cep.replace("-", "")
            if len(cep_formatado) < 8:
                logger.info("CEP com tamanho menor que 8")
                return None
            return cep

        def validate_city(cidade: str, longitude, latitude) -> bool:
            if cidade and cidade != "Rio de Janeiro":
                logger.info("O município do endereço é diferente de Rio de Janeiro")
                return True
            
            if not cidade and self.shape_rj:
                logger.info("Não foi identificado um município para esse endereço")
                point = Point(
                    float(longitude),
                    float(latitude),
                )
                if not self.shape_rj.contains(point):
                    logger.info("O endereço identificado está fora do Rio de Janeiro")
                    return True
                
            return False

        ACCEPTED_LOGRADOUROS = [
            "route",
            "establishment",
            "street_address",
            "town_square",
            "point_of_interest",
        ]

        async with aiohttp.ClientSession() as maps_session:
            client = AsyncClient(maps_session, key=env.GMAPS_API_TOKEN)
            geocode_result = await client.geocode(address)

        logger.info(f"GEOCODE RESULT:\n{geocode_result}")

        if len(geocode_result) == 0:
            return {"valid": False, "error": "Endereço não encontrado"}

        if geocode_result[0].get("formatted_address") is None:
            logger.info("no geocode result")
            lat, long = geocode_result[0]["geometry"]["location"].values()
            async with aiohttp.ClientSession() as maps_session:
                client = AsyncClient(maps_session, key=env.GMAPS_API_TOKEN)
                geocode_result = await client.reverse_geocode((lat, long))

        logradouro, resultado = set_logradouro(geocode_result, ACCEPTED_LOGRADOUROS)
        if not logradouro:
            return {"valid": False, "error": "Logradouro não identificado"}

        address_info = set_additional_info(resultado, ACCEPTED_LOGRADOUROS)

        logradouro_lat = resultado["geometry"]["location"]["lat"]
        logradouro_long = resultado["geometry"]["location"]["lng"]

        logger.info(f'Lat, Long: {logradouro_lat}, {logradouro_long}')

        logradouro_fora_do_rj = validate_city(address_info.get("cidade"), logradouro_long, logradouro_lat)
        if logradouro_fora_do_rj:
            return {"valid": False, "error": "O endereço informado está fora do município do Rio de Janeiro"}

        try:
            numero = address_info.get("numero", "")
            numero = numero.split(".")[0]
        except:  # noqa
            logger.info("logradouro_numero: falhou ao tentar pegar a parcela antes do `.`")
            numero = ""

        return {
            "valid": True,
            "latitude": logradouro_lat,
            "longitude": logradouro_long,
            "logradouro": address_info.get("logradouro", ""),
            "numero": numero,
            "bairro": address_info.get("bairro", ""),
            "cep": address_info.get("cep", ""),
            "cidade": address_info.get("cidade", "Rio de Janeiro"),
            "estado": address_info.get("estado", "RJ"),
            "formatted_address": resultado.get("formatted_address", "")
        }
    
    def get_nearest_logradouro_and_bairro(self, latitude: float, longitude: float) -> NearestLocation:
        """
        Get the nearest logradouro and bairro to a given latitude and longitude.

        Args:
            latitude (float): The latitude.
            longitude (float): The longitude.

        Returns:
            NearestLocation: The nearest logradouro and bairro.
        """

        logradouros = pd.read_json(env.DATA_DIR / "logradouros.json")
        logradouros["geometry"] = logradouros["geometry"].apply(loads)
        bairros = pd.read_json(env.DATA_DIR / "bairros.json")
        bairros["geometry"] = bairros["geometry"].apply(loads)
        
        query_point = Point(longitude, latitude)

        logradouros_copy = logradouros.copy(deep=True)
        bairros_copy = bairros.copy(deep=True)

        logradouros_copy["distance"] = logradouros_copy["geometry"].apply(lambda x: x.distance(query_point))
        bairros_copy["distance"] = bairros_copy["geometry"].apply(lambda x: x.distance(query_point))

        nearest_logradouro = logradouros_copy.loc[logradouros_copy["distance"].idxmin()]
        nearest_bairro = bairros_copy.loc[bairros_copy["distance"].idxmin()]

        return NearestLocation(
            id_logradouro=nearest_logradouro["id"],
            name_logradouro=nearest_logradouro["nome"],
            id_bairro=nearest_bairro["id"],
            name_bairro=nearest_bairro["nome"],
        )
    
    @interceptor(source={"source": "mcp", "tool": "multi_step_service", "workflow": "poda_de_arvore"})
    async def get_endereco_info(self, latitude, longitude, logradouro_google=None, bairro_google=None) -> dict:
        try:
            latitude = float(latitude)
            longitude = float(longitude)

            nearest_location: NearestLocation = self.get_nearest_logradouro_and_bairro(latitude, longitude)

            logradouro_id_ipp = str(nearest_location.id_logradouro)
            bairro_id_ipp = str(nearest_location.id_bairro)
            logradouro_nome_ipp = str(nearest_location.name_logradouro)
            bairro_nome_ipp = str(nearest_location.name_bairro)

            logger.info(f'Código bairro obtido: {bairro_id_ipp}')
            logger.info(f'Nome bairro obtido: {bairro_nome_ipp}')
        except Exception as e:
            logger.info(f"Falha ao obter endereço usando get_nearest_logradouro_and_bairro(): {e}")
            bairro_id_ipp = "0"
            logradouro_id_ipp = "0"
            logradouro_nome_ipp = " "
            bairro_nome_ipp = " "

        try:
            if not bairro_nome_ipp or bairro_id_ipp == "0":
                logger.info("Geolocalização não retornou bairro")
                bairro_nome_ipp = " "

            # Se temos logradouro do Google, tenta obter código IPP mais preciso
            if logradouro_google:
                logger.info("Chamando função que identifica o logradouro por similaridade de texto")
                ipp_result = await self.get_ipp_street_code(
                    logradouro_nome=logradouro_google,
                    logradouro_nome_ipp=logradouro_nome_ipp,
                    bairro_nome_ipp=bairro_nome_ipp,
                    latitude=latitude,
                    longitude=longitude,
                    bairro_google=bairro_google
                )
                
                # Se conseguiu resultado melhor do IPP, usa ele
                if ipp_result and not ipp_result.get("error"):
                    return ipp_result

            # Retorna dados básicos se não conseguiu melhorar com IPP
            return {
                "logradouro_id": logradouro_id_ipp,
                "logradouro_nome": logradouro_nome_ipp,
                "bairro_id": bairro_id_ipp,
                "bairro_nome": bairro_nome_ipp,
                "latitude": latitude,
                "longitude": longitude
            }
        except Exception as e:
            logger.info(f"Erro ao processar endereço: {e}")
            return {
                "error": str(e),
                "abertura_manual": True
            }
        
    async def substitute_digits(self, address: str) -> str:
        pattern = re.compile(r"\d+")
        return pattern.sub(lambda x: num2words(int(x.group()), lang="pt-br"), address)


    def haversine_distance(self, lat1, lon1, lat2, lon2):
        lat1 = float(lat1) if isinstance(lat1, str) else lat1
        lon1 = float(lon1) if isinstance(lon1, str) else lon1
        lat2 = float(lat2) if isinstance(lat2, str) else lat2
        lon2 = float(lon2) if isinstance(lon2, str) else lon2

        # Raio médio da Terra em quilômetros
        R = 6371.0

        # Converte as coordenadas de graus para radianos
        lat1_rad = math.radians(lat1)
        lon1_rad = math.radians(lon1)
        lat2_rad = math.radians(lat2)
        lon2_rad = math.radians(lon2)

        # Diferenças entre as coordenadas
        dlat = lat2_rad - lat1_rad
        dlon = lon2_rad - lon1_rad

        # Fórmula de Haversine
        a = (
            math.sin(dlat / 2) ** 2
            + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon / 2) ** 2
        )
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

        # Distância em quilômetros
        distance_km = R * c

        # Distância em metros
        distance_m = distance_km * 1000
        return distance_m
    
    @interceptor(source={"source": "mcp", "tool": "multi_step_service", "workflow": "poda_de_arvore"})
    async def get_ipp_street_code(self, logradouro_nome, logradouro_nome_ipp, bairro_nome_ipp, latitude, longitude, bairro_google=None) -> dict:
        THRESHOLD = 0.8
        logradouro_google = logradouro_nome
        logradouro_ipp = logradouro_nome_ipp.lstrip("0123456789 -")
        logradouro_completo = f'{logradouro_google}, {bairro_nome_ipp}'

        logger.info(f"Logradouro IPP: {logradouro_ipp}")

        address_similarity = jaro_similarity(logradouro_google, logradouro_ipp)

        if (address_similarity > THRESHOLD) and bairro_nome_ipp != " ":
            logger.info(f"Similaridade alta o suficiente: {address_similarity}")
            geocode_logradouro_ipp_url = str(
                "https://pgeo3.rio.rj.gov.br/arcgis/rest/services/Geocode/Geocode_Logradouros_WGS84/GeocodeServer/findAddressCandidates?"
                + f"Address={logradouro_completo}&Address2=&Address3=&Neighborhood=&City=&Subregion=&Region=&Postal=&PostalExt=&CountryCode=&SingleLine=&outFields=cl"
                + "&maxLocations=&matchOutOfRange=true&langCode=&locationType=&sourceCountry=&category=&location=&searchExtent=&outSR=&magicKey=&preferredLabelValues=&f=pjson"
            )
            logger.info(f"Geocode IPP URL: {geocode_logradouro_ipp_url}")
            return

        if address_similarity < THRESHOLD:
            logger.info(f"logradouro_nome retornado pelo Google significantemente diferente do retornado pelo IPP. Threshold: {address_similarity}")
            logradouro_google = await self.substitute_digits(logradouro_google)
            if bairro_nome_ipp == " ":
                logger.info("Além dos endereços serem muito diferentes, não há bairro IPP. Então vou considerar o bairro do Google.")
                if bairro_google:
                    logradouro_completo = f'{logradouro_google}, {bairro_google}'
                else:
                    logradouro_completo = logradouro_google
        elif bairro_nome_ipp == " ":
            logger.info(f"Bairro IPP não identificado. Valor Bairro IPP: {bairro_nome_ipp}. Vou considerar o do Google.")
            logger.info("Atualizando o logradouro que vai ser geolocalizado para considerar o logradouro_ipp em vez do Google")
            if bairro_google:
                logradouro_completo = f'{logradouro_ipp}, {bairro_google}'
            else:
                logradouro_completo = logradouro_ipp

        # Call IPP api
        geocode_logradouro_ipp_url = str(
            "https://pgeo3.rio.rj.gov.br/arcgis/rest/services/Geocode/Geocode_Logradouros_WGS84/GeocodeServer/findAddressCandidates?"
            + f"Address={logradouro_completo}&Address2=&Address3=&Neighborhood=&City=&Subregion=&Region=&Postal=&PostalExt=&CountryCode=&SingleLine=&outFields=cl"
            + "&maxLocations=&matchOutOfRange=true&langCode=&locationType=&sourceCountry=&category=&location=&searchExtent=&outSR=&magicKey=&preferredLabelValues=&f=pjson"
        )
        logger.info(f"Geocode IPP URL: {geocode_logradouro_ipp_url}")

        async with InterceptedHTTPClient(
            user_id="unknown",
            source={"source": "mcp", "tool": "multi_step_service", "workflow": "poda_de_arvore", "function": "get_ipp_street_code"},
            timeout=30.0
        ) as client:
            response = await client.get(geocode_logradouro_ipp_url)
            data = response.json()
        try:
            candidates = list(data["candidates"])
            logradouro_codigo = None
            logradouro_real = None

            if bairro_nome_ipp == " ":
                best_distance = 1000000000
                logger.info(f'Logradouro será o mais próximo do lat/long retornado pelo Google. Lat:{latitude} | Long:{longitude}')
                for candidato in candidates:
                    distance = self.haversine_distance(latitude, longitude, candidato["location"]["y"], candidato["location"]["x"])
                    if distance < best_distance and "," in candidato["address"]:
                        best_distance = distance
                        logradouro_codigo = candidato["attributes"]["cl"]
                        logradouro_real = candidato["address"]
                logger.info(f"Logradouro no IPP com maior semelhança: {logradouro_real}, cl: {logradouro_codigo}, distância: {best_distance} metros")
            else:
                best_similarity = 0
                logger.info("Logradouro será selecionado pela similaridade de texto")
                for candidato in candidates:
                    similarity = jaro_similarity(candidato["address"], logradouro_completo)
                    if similarity > best_similarity and "," in candidato["address"]:
                        best_similarity = similarity
                        logradouro_codigo = candidato["attributes"]["cl"]
                        logradouro_real = candidato["address"]
                logger.info(f"Logradouro no IPP com maior similaridade: {logradouro_real}, cl: {logradouro_codigo}, similaridade: {best_similarity}")
            logger.info(f"Logradouro encontrado no Google, com bairro do IPP: {logradouro_completo}")

            logradouro_id_ipp = logradouro_codigo
            logradouro_nome_ipp = logradouro_real.split(",")[0]

            try:
                best_candidate_bairro_nome_ipp = logradouro_real.split(",")[1][1:]
            except:  # noqa: E722
                logger.info("Logradouro no IPP com maior semelhança não possui bairro no nome")
                logradouro_bairro_ipp = None

            if best_candidate_bairro_nome_ipp and 'logradouro_bairro_ipp' in locals() and logradouro_bairro_ipp and (jaro_similarity(best_candidate_bairro_nome_ipp, logradouro_bairro_ipp) > THRESHOLD):
                logger.info(f"Similaridade entre bairro atual e bairro do Logradouro no IPP com maior semelhança é alta o suficiente")
                return {
                    "logradouro_id": logradouro_id_ipp,
                    "logradouro_nome": logradouro_nome_ipp,
                    "bairro_nome": bairro_nome_ipp
                } 
            else:
                # Se o bairro do endereço com maior similaridade for diferente do que coletamos usando geolocalização,
                # pegamos o codigo correto buscando o nome do bairro desse endereço na base do IPP e pegando o codigo correspondente
                logger.info("Foi necessário atualizar o bairro")
                logger.info(f'Bairro obtido anteriormente com geolocalização: {bairro_nome_ipp if "bairro_nome_ipp" in locals() else "não definido"}')

                sgrc_service = SGRCAPIService()
                url = sgrc_service.get_integrations_url("neighborhood_id")
                payload = json.dumps({"name": best_candidate_bairro_nome_ipp})
                key = env.CHATBOT_INTEGRATIONS_KEY
                headers = {
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {key}",
                }

                async with InterceptedHTTPClient(
                    user_id="unknown",
                    source={"source": "mcp", "tool": "multi_step_service", "workflow": "poda_de_arvore", "function": "get_ipp_street_code"},
                    timeout=30.0
                ) as client:
                    response = await client.post(url, headers=headers, content=payload)
                    response_json = response.json()
                    logradouro_id_bairro_ipp = response_json["id"]
                    logradouro_bairro_ipp = response_json["name"]

                logger.info(f'Bairro obtido agora com busca por similaridade: {logradouro_bairro_ipp}')
                
                return {
                    "logradouro_id": logradouro_id_ipp,
                    "logradouro_nome": logradouro_nome_ipp,
                    "bairro_id": logradouro_id_bairro_ipp,
                    "bairro_nome": logradouro_bairro_ipp
                }
        except Exception as e:
            logger.info(f"Erro ao buscar código IPP: {e}")
            return {
                "error": str(e)
            } 
