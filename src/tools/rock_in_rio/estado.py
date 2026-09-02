"""Veredito do último ciclo de busca do line-up, para quem observa de fora.

Existe por causa de um ponto cego concreto: em 01/09/2026 o site mudou de
estrutura, a tool ficou fora do ar e **nada** acusou — a suíte de testes roda
sobre fixtures salvas e ficou verde, o `/health/detail` não tinha check de
line-up, e o SigNoz não viu erro nenhum porque `get_rock_in_rio_lineup`
captura a exceção e devolve um dicionário (um `return` normal, para o
middleware de tracing, é sucesso). Quem achou a quebra foi uma pessoa olhando
a URL na mão.

Este módulo é o registro que faltava. Ele não sonda nada: quem escreve é o
laço de atualização que já existe em `cache.py`, e quem lê é o health check em
`src/health/checks.py`.

Mora num módulo próprio, e não dentro de `cache.py`, justamente por causa do
leitor: importar `cache.py` arrastaria o scraper e o `InterceptedHTTPClient`
para dentro do módulo de health, contra o isolamento que `src/health/checks.py`
documenta manter no próprio docstring.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

# Os dois modos de falha, separados porque a ação é oposta. Um alerta que não
# diz qual dos dois aconteceu não diz o que fazer.
FALHA_FORMATO = "formato"  # o HTML mudou: exige correção de código
FALHA_FONTE = "fonte"  # rede/HTTP: transitório, tende a passar sozinho


@dataclass(frozen=True)
class ResultadoDoCiclo:
    """O que aconteceu na última ida à fonte."""

    sucesso: bool
    em: float
    atracoes: int = 0
    palcos: int = 0
    falha: Optional[str] = None
    detalhe: Optional[str] = None

    @property
    def idade_s(self) -> float:
        return max(0.0, time.time() - self.em)


_ultimo: Optional[ResultadoDoCiclo] = None


def registrar_sucesso(*, atracoes: int, palcos: int) -> ResultadoDoCiclo:
    global _ultimo
    _ultimo = ResultadoDoCiclo(
        sucesso=True, em=time.time(), atracoes=atracoes, palcos=palcos
    )
    return _ultimo


def registrar_falha(*, falha: str, detalhe: str) -> ResultadoDoCiclo:
    global _ultimo
    _ultimo = ResultadoDoCiclo(
        sucesso=False, em=time.time(), falha=falha, detalhe=detalhe
    )
    return _ultimo


def ultimo_resultado() -> Optional[ResultadoDoCiclo]:
    """O último ciclo, ou `None` se nenhum completou ainda.

    `None` é diferente de falha: significa "ainda não sei", e o health check
    precisa dessa distinção para devolver `SKIPPED` em vez de `DOWN` num pod
    que acabou de subir.
    """
    return _ultimo


def resetar() -> None:
    """Descarta o veredito. Existe para isolamento entre testes."""
    global _ultimo
    _ultimo = None
