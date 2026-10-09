import pytest

from mining_server.domain.core import DomainError
from mining_server.domain.documents import QuantityUnit, ResourceQuantity
from mining_server.infrastructure.documents.agent import DocumentAgent
from mining_server.infrastructure.documents.parser import PdfParser


@pytest.mark.parametrize("material", ["ore", "mineralized material"])
def test_tonnage_resource_mass_does_not_require_a_literal_ore_label(material):
    agent = DocumentAgent(None, PdfParser(), "gpt-6.1-sol")
    quantity = ResourceQuantity(
        value=378, unit=QuantityUnit.MEGATONNE, material=material
    )
    agent.verify_quantity(quantity, "Tonnes (Mt) Indicated 378", is_tonnage=True)


@pytest.mark.parametrize("material", ["Li", "Li2O", "Li2CO3"])
def test_contained_material_cannot_be_used_as_resource_tonnage(material):
    agent = DocumentAgent(None, PdfParser(), "gpt-6.1-sol")
    quantity = ResourceQuantity(
        value=378, unit=QuantityUnit.MEGATONNE, material=material
    )
    with pytest.raises(DomainError):
        agent.verify_quantity(
            quantity, f"Tonnes (Mt) Indicated 378 {material}", is_tonnage=True
        )


def test_resource_mass_semantics_do_not_relax_contained_material_evidence():
    agent = DocumentAgent(None, PdfParser(), "gpt-6.1-sol")
    quantity = ResourceQuantity(
        value=378, unit=QuantityUnit.MEGATONNE, material="mineralized material"
    )
    with pytest.raises(DomainError):
        agent.verify_quantity(quantity, "Tonnes (Mt) Indicated 378")
