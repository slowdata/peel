"""Interface base para todas as fontes de curadoria.

Decisão: usar ABC (Abstract Base Class) para forçar a interface.
Razão: qualquer Source que esqueça implementar fetch() crasha ao instanciar,
não a meio da run. Filosofia: uma interface, muitas implementações
(RSS, Spotify playlist, scraping HTML, etc.).
"""

from abc import ABC, abstractmethod

from peel.models import Track


class Source(ABC):
    """Interface base para fontes de curadoria de música."""

    id: str
    """Identificador estável da fonte, ex.: 'pitchfork_bnt' ou 'bbc6music'."""

    name: str
    """Nome human-readable da fonte, ex.: 'Pitchfork Best New Tracks'."""

    @abstractmethod
    def fetch(self) -> list[Track]:
        """Vai buscar o estado actual da fonte.

        Returns:
            Lista de Track novos/atualizados. Vazio se nenhum, mas nunca None.

        Notas:
        - Idempotente: correr 2x com os mesmos dados retorna a mesma coisa.
        - Deduplicação acontece em main.py, não aqui.
        - Se houver erro (site em baixo, parse quebrado), levanta exceção.
          main.py trata.
        """
        ...
