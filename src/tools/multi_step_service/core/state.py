import os
import json
import asyncio
from pathlib import Path
from enum import Enum
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, Optional

from loguru import logger

from src.tools.multi_step_service.core.models import ServiceState, ServiceMetadata
from src.config import env

try:
    import redis.asyncio as redis
except ImportError:
    redis = None

try:
    import aiofiles
except ImportError:
    aiofiles = None


class StateMode(Enum):
    """Modos de persistência disponíveis."""

    JSON = "json"
    REDIS = "redis"
    BOTH = "both"


class StorageBackend(ABC):
    """Interface abstrata para backends de persistência (async)."""

    @abstractmethod
    async def load_user_data(self, user_id: str) -> Dict[str, Any]:
        """Carrega todos os dados de um usuário."""
        pass

    @abstractmethod
    async def save_user_data(self, user_id: str, data: Dict[str, Any]) -> None:
        """Salva todos os dados de um usuário."""
        pass

    @abstractmethod
    async def remove_user_data(self, user_id: str) -> bool:
        """Remove todos os dados de um usuário."""
        pass

    @abstractmethod
    async def health_check(self) -> bool:
        """Verifica se o backend está acessível."""
        pass


class JsonBackend(StorageBackend):
    """
    Backend de persistência usando arquivos JSON locais (async).
    Armazena em: {data_dir}/{user_id}.json
    """

    def __init__(self, data_dir: str = "src/tools/multi_step_service/data"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(exist_ok=True)

    def _get_file_path(self, user_id: str) -> Path:
        return self.data_dir / f"{user_id}.json"

    async def load_user_data(self, user_id: str) -> Dict[str, Any]:
        file_path = self._get_file_path(user_id)
        if file_path.exists():
            if aiofiles:
                async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                    content = await f.read()
                    return json.loads(content)
            else:
                # Fallback para I/O síncrono se aiofiles não disponível
                with open(file_path, "r", encoding="utf-8") as f:
                    return json.load(f)
        return {}

    async def save_user_data(self, user_id: str, data: Dict[str, Any]) -> None:
        file_path = self._get_file_path(user_id)
        content = json.dumps(data, ensure_ascii=False, indent=2)

        if aiofiles:
            async with aiofiles.open(file_path, "w", encoding="utf-8") as f:
                await f.write(content)
        else:
            # Fallback para I/O síncrono se aiofiles não disponível
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)

    async def remove_user_data(self, user_id: str) -> bool:
        file_path = self._get_file_path(user_id)
        if file_path.exists():
            os.remove(file_path)
            return True
        return False

    async def health_check(self) -> bool:
        try:
            return self.data_dir.exists() and os.access(self.data_dir, os.W_OK)
        except Exception:
            return False


class RedisBackend(StorageBackend):
    """
    Backend de persistência usando Redis (async).
    Suporta URLs no formato: redis://:password@host:port/db
    Chaves: user_id

    Reconexão/retry (Task 6 do plano de resiliência do MCP):
    O Redis em produção é servido por Sentinel + HAProxy (infra/superapp,
    Task 5): quando o primary cai, o Sentinel demora até
    `down-after-milliseconds` (10s, ver
    `modules/deployments/yamls/redis-ha-mcp/values.yaml` no repo de infra)
    para declará-lo indisponível e promover uma réplica; o HAProxy então
    redireciona `mcp-redis:6379` para o novo primary. Durante essa janela o
    cliente recebe `redis.ConnectionError` do mesmo endpoint estável
    (`REDIS_URL` não muda — não há descoberta de Sentinel/cluster aqui).

    Este backend absorve essa janela com um orçamento de retry exponencial
    limitado: apenas `redis.ConnectionError` é retentado (outros erros, como
    de sintaxe de comando, não se resolvem tentando de novo e propagam na
    hora). Esgotado o orçamento, o `ConnectionError` original é relançado —
    o mesmo tipo que os chamadores (`CompositeBackend`, `health_check`) já
    tratavam antes desta mudança, então nenhum caminho de erro existente
    precisou mudar.
    """

    # Tentativas totais (1 inicial + N retentativas) e backoff exponencial
    # com teto. Com os padrões abaixo: 1s, 2s, 4s, 8s de espera acumulada
    # (~15s) antes de desistir — acima dos 10s de `down-after-milliseconds`
    # do Sentinel, com margem para a promoção da réplica e a atualização do
    # HAProxy. Não depende de tempo real em teste: os testes injetam
    # `max_attempts`/`base_delay_seconds` menores e substituem
    # `asyncio.sleep`.
    DEFAULT_MAX_ATTEMPTS = 5
    DEFAULT_BASE_DELAY_SECONDS = 1.0
    DEFAULT_MAX_DELAY_SECONDS = 8.0

    def __init__(
        self,
        redis_url: str,
        ttl_seconds: Optional[int] = None,
        max_attempts: Optional[int] = None,
        base_delay_seconds: Optional[float] = None,
        max_delay_seconds: Optional[float] = None,
    ):
        """
        Inicializa backend Redis a partir de uma URL (async).

        Args:
            redis_url: URL no formato redis://:password@host:port/db
                      Exemplos:
                      - redis://localhost:6379/0
                      - redis://:mypassword@localhost:6379/0
                      - redis://host:6379
            ttl_seconds: TTL aplicado às chaves gravadas (opcional).
            max_attempts: Tentativas totais por operação diante de
                `redis.ConnectionError` (padrão `DEFAULT_MAX_ATTEMPTS`).
            base_delay_seconds: Espera antes da 2ª tentativa; dobra a cada
                nova falha (padrão `DEFAULT_BASE_DELAY_SECONDS`).
            max_delay_seconds: Teto da espera exponencial (padrão
                `DEFAULT_MAX_DELAY_SECONDS`).

        Raises:
            ImportError: Se biblioteca redis não estiver instalada
        """
        if redis is None:
            raise ImportError(
                "Biblioteca 'redis' não instalada. Instale com: uv add redis"
            )

        # Usa from_url do redis.asyncio que já faz todo o parsing
        self.ttl_seconds = ttl_seconds
        self.max_attempts = max_attempts or self.DEFAULT_MAX_ATTEMPTS
        self.base_delay_seconds = (
            base_delay_seconds
            if base_delay_seconds is not None
            else self.DEFAULT_BASE_DELAY_SECONDS
        )
        self.max_delay_seconds = (
            max_delay_seconds
            if max_delay_seconds is not None
            else self.DEFAULT_MAX_DELAY_SECONDS
        )
        self.client = redis.Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
        )

    def _get_key(self, user_id: str) -> str:
        return user_id

    async def _call_with_retry(
        self, operation: str, func: Callable[..., Any], *args, **kwargs
    ):
        """Executa uma chamada Redis com retry exponencial limitado.

        Só `redis.ConnectionError` é retentado — é o sintoma da janela de
        failover do Sentinel/HAProxy. Qualquer outro erro propaga na
        primeira falha, sem retry nem sleep. Esgotado `self.max_attempts`,
        o `ConnectionError` original é relançado (não é convertido nem
        engolido).
        """
        # `__init__` já falhou com ImportError se `redis` estivesse None;
        # nenhuma instância existe sem essa garantia.
        assert redis is not None
        last_error: Optional[Exception] = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                return await func(*args, **kwargs)
            except redis.ConnectionError as exc:
                last_error = exc
                if attempt >= self.max_attempts:
                    logger.error(
                        f"Redis {operation}: orçamento de {self.max_attempts} "
                        f"tentativa(s) esgotado, desistindo. Último erro: {exc}"
                    )
                    raise
                delay = min(
                    self.base_delay_seconds * (2 ** (attempt - 1)),
                    self.max_delay_seconds,
                )
                logger.warning(
                    f"Redis {operation}: tentativa {attempt}/{self.max_attempts} "
                    f"falhou com ConnectionError ({exc}); nova tentativa em "
                    f"{delay}s"
                )
                await asyncio.sleep(delay)

        if last_error is not None:
            raise last_error
        raise RuntimeError(
            f"Redis {operation}: loop de retry terminou sem executar "
            f"nenhuma tentativa (max_attempts={self.max_attempts})"
        )

    async def load_user_data(self, user_id: str) -> Dict[str, Any]:
        key = self._get_key(user_id)
        data = await self._call_with_retry("get", self.client.get, key)  # type: ignore[arg-type]
        if data:
            return json.loads(data)  # type: ignore[arg-type]
        return {}

    async def save_user_data(self, user_id: str, data: Dict[str, Any]) -> None:
        key = self._get_key(user_id)
        serialized = json.dumps(data, ensure_ascii=False)
        await self._call_with_retry(
            "set", self.client.set, name=key, value=serialized, ex=self.ttl_seconds
        )

    async def remove_user_data(self, user_id: str) -> bool:
        key = self._get_key(user_id)
        deleted = await self._call_with_retry("delete", self.client.delete, key)
        return deleted > 0  # type: ignore[operator]

    async def health_check(self) -> bool:
        try:
            result = await self._call_with_retry("ping", self.client.ping)  # type: ignore[misc]
            return bool(result)
        except Exception:
            return False

    async def close(self):
        """Fecha a conexão com o Redis."""
        await self.client.aclose()  # type: ignore[attr-defined]


class CompositeBackend(StorageBackend):
    """
    Backend composto que usa múltiplos backends simultaneamente (async).

    Estratégia:
    - Leitura: Tenta Redis primeiro, fallback para JSON
    - Escrita: Salva em ambos (paralelo com asyncio.gather)
    - Remoção: Remove de ambos (paralelo com asyncio.gather)
    """

    def __init__(self, redis_backend: RedisBackend, json_backend: JsonBackend):
        self.redis = redis_backend
        self.json = json_backend

    async def load_user_data(self, user_id: str) -> Dict[str, Any]:
        # Tenta Redis primeiro (mais rápido)
        try:
            if await self.redis.health_check():
                data = await self.redis.load_user_data(user_id)
                if data:
                    return data
        except Exception:
            pass

        # Fallback para JSON
        return await self.json.load_user_data(user_id)

    async def save_user_data(self, user_id: str, data: Dict[str, Any]) -> None:
        # Salva em ambos em paralelo usando asyncio.gather
        import asyncio

        errors = []

        # Salvar em JSON
        async def save_json():
            try:
                await self.json.save_user_data(user_id, data)
            except Exception as e:
                errors.append(f"JSON: {e}")

        # Salvar em Redis
        async def save_redis():
            try:
                await self.redis.save_user_data(user_id, data)
            except Exception as e:
                errors.append(f"Redis: {e}")

        # Executa ambos em paralelo
        await asyncio.gather(save_json(), save_redis(), return_exceptions=True)

        # Se ambos falharam, levanta erro
        if len(errors) == 2:
            raise Exception(f"Falha ao salvar em ambos backends: {', '.join(errors)}")

    async def remove_user_data(self, user_id: str) -> bool:
        import asyncio

        json_removed = False
        redis_removed = False

        # Remover de JSON
        async def remove_json():
            nonlocal json_removed
            try:
                json_removed = await self.json.remove_user_data(user_id)
            except Exception:
                pass

        # Remover de Redis
        async def remove_redis():
            nonlocal redis_removed
            try:
                redis_removed = await self.redis.remove_user_data(user_id)
            except Exception:
                pass

        # Executa ambos em paralelo
        await asyncio.gather(remove_json(), remove_redis(), return_exceptions=True)

        return json_removed or redis_removed

    async def health_check(self) -> bool:
        # Pelo menos um deve estar saudável
        import asyncio

        results = await asyncio.gather(
            self.json.health_check(),
            self.redis.health_check(),
            return_exceptions=True,
        )
        return any(r for r in results if isinstance(r, bool) and r)


class StateManager:
    """
    Gerenciador de estado responsável por salvar, carregar, atualizar e remover dados.

    Suporta três modos de persistência:
    - JSON: Apenas arquivos locais (padrão)
    - REDIS: Apenas Redis
    - BOTH: Redis + JSON simultaneamente

    O modo é configurado no construtor via parâmetro backend_mode.
    """

    def __init__(
        self,
        user_id: str = "agent",
        data_dir: str = "src/tools/multi_step_service/data",
        backend_mode: StateMode = StateMode.JSON,
        redis_url: Optional[str] = None,
        redis_ttl_seconds: Optional[int] = None,
    ):
        """
        Inicializa o StateManager.

        Args:
            user_id: ID do usuário
            data_dir: Diretório para arquivos JSON (usado em JSON e BOTH)
            backend_mode: Modo de persistência (JSON, REDIS, BOTH)
            redis_url: URL Redis no formato redis://:password@host:port/db
                      Se None, usa REDIS_URL da env (apenas para REDIS/BOTH)

        Raises:
            ValueError: Se backend_mode for REDIS/BOTH e redis_url não fornecido
        """
        self.user_id = user_id
        self.backend_mode = backend_mode
        self.backend = self._create_backend(
            data_dir, backend_mode, redis_url, redis_ttl_seconds
        )

    def _create_backend(
        self,
        data_dir: str,
        mode: StateMode,
        redis_url: Optional[str],
        redis_ttl_seconds: Optional[int],
    ) -> StorageBackend:
        """Cria o backend apropriado baseado no modo."""

        if mode == StateMode.JSON:
            return JsonBackend(data_dir=data_dir)

        elif mode == StateMode.REDIS:
            url = redis_url or env.REDIS_URL
            ttl_seconds = redis_ttl_seconds or env.REDIS_TTL_SECONDS
            if not url:
                raise ValueError(
                    "StateMode.REDIS requer redis_url ou REDIS_URL configurado"
                )
            return RedisBackend(redis_url=url, ttl_seconds=ttl_seconds)

        elif mode == StateMode.BOTH:
            url = redis_url or env.REDIS_URL
            ttl_seconds = redis_ttl_seconds or env.REDIS_TTL_SECONDS
            if not url:
                raise ValueError(
                    "StateMode.BOTH requer redis_url ou REDIS_URL configurado"
                )
            json_backend = JsonBackend(data_dir=data_dir)
            redis_backend = RedisBackend(redis_url=url, ttl_seconds=ttl_seconds)
            return CompositeBackend(redis_backend, json_backend)

        else:
            raise ValueError(f"StateMode inválido: {mode}")

    async def _load_user_data(self) -> Dict[str, Any]:
        """Carrega todos os dados do usuário usando o backend configurado (async)."""
        return await self.backend.load_user_data(self.user_id)

    async def _save_user_data(self, data: Dict[str, Any]) -> None:
        """Salva todos os dados do usuário usando o backend configurado (async)."""
        await self.backend.save_user_data(self.user_id, data)

    async def load_service_state(self, service_name: str) -> Optional[ServiceState]:
        """Carrega o estado de um serviço específico (async)."""
        user_data = await self._load_user_data()
        service_data = user_data.get(service_name)

        if service_data:
            # Compatibilidade: Se não tem metadata, cria um novo
            if "metadata" not in service_data:
                service_data["metadata"] = ServiceMetadata().model_dump()

            return ServiceState(
                user_id=self.user_id, service_name=service_name, **service_data
            )
        return None

    async def save_service_state(self, state: ServiceState) -> None:
        """Salva o estado de um serviço (async)."""
        # Auto-atualiza o timestamp de updated_at antes de salvar
        state.metadata.update_timestamp()

        user_data = await self._load_user_data()

        # Atualiza apenas os dados do serviço específico
        user_data[state.service_name] = {
            "status": state.status,
            "data": state.data,
            "internal": state.internal,
            "metadata": state.metadata.model_dump(mode="json"),
        }

        await self._save_user_data(user_data)

    async def update_service_state(
        self, service_name: str, updates: Dict[str, Any]
    ) -> None:
        """Atualiza campos específicos do estado de um serviço (async)."""
        state = await self.load_service_state(service_name)

        if state is None:
            # Criar novo estado se não existir
            state = ServiceState(user_id=self.user_id, service_name=service_name)

        # Aplicar atualizações
        for key, value in updates.items():
            if hasattr(state, key):
                setattr(state, key, value)

        await self.save_service_state(state)

    async def remove_service_state(self, service_name: str) -> bool:
        """Remove o estado de um serviço específico (async)."""
        user_data = await self._load_user_data()

        if service_name in user_data:
            del user_data[service_name]
            await self._save_user_data(user_data)
            return True
        return False

    async def remove_user_data(self) -> bool:
        """Remove todos os dados do usuário usando o backend configurado (async)."""
        return await self.backend.remove_user_data(self.user_id)
