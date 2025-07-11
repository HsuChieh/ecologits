import os
from csv import DictReader
from dataclasses import dataclass
from typing import Optional


@dataclass
class ElectricityMix:
    """
    Electricity mix of a country

    Attributes:
        zone: ISO 3166-1 alpha-3 code of the electricity mix zone
        adpe: Abiotic Depletion Potential of the mix (in kgSbeq / kWh)
        pe: Primary Energy of the mix (in MJ / kWh)
        gwp: Global Warming Potential of the mix (in kgCO2eq / kWh)
    """
    zone: str
    adpe: float
    pe: float
    gwp: float
    wcf: float


class ElectricityMixRepository:
    """
    Repository of electricity mixes.
    """

    def __init__(self, electricity_mixes: list[ElectricityMix]) -> None:
        self.__electricity_mixes = electricity_mixes

    def find_electricity_mix(self, zone: str) -> Optional[ElectricityMix]:
        for electricity_mix in self.__electricity_mixes:
            if electricity_mix.zone == zone:
                return electricity_mix
        return None

    @classmethod
    def from_csv(cls, filepath: Optional[str] = None) -> "ElectricityMixRepository":
        if filepath is None:
            filepath = os.path.join(
                os.path.dirname(os.path.realpath(__file__)), "data", "electricity_mixes.csv"
            )
        electricity_mixes = []
        with open(filepath) as fd:
            csv = DictReader(fd)
            for row in csv:
                if row["name"].upper() == "WOR":
                    wcf_wor_value = row.get("wcf", "")
                wcf_value = row.get("wcf", "") # enlève les espaces éventuels
                wcf = float(wcf_value) if wcf_value else wcf_wor_value
                #TODO : n'oublie pas d'afficher un message de avertissement si la valeur qu'on prend est un zéro (on utilise la moyenne mondiale dans ce cas)

                electricity_mixes.append(
                    ElectricityMix(
                        zone=row["name"],
                        adpe=float(row["adpe"]),
                        pe=float(row["pe"]),
                        gwp=float(row["gwp"]),
                        wcf=wcf
                    )
                )
        return cls(electricity_mixes)


electricity_mixes = ElectricityMixRepository.from_csv()
