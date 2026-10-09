from pathlib import Path

import pytest
from pydantic import ValidationError

from mining_server.domain.documents import (
    GoldenReport,
    GoldenReports,
    GoldenResourceExpectation,
    PageEvidence,
    ResourceQuantity,
    ResourceRecord,
    ResourceScope,
)
from mining_server.infrastructure.documents.golden import matches_record

GOLDEN = GoldenReports.model_validate_json(
    (Path(__file__).parent / "fixtures/document-golden.json").read_text(
        encoding="utf-8"
    )
)


def resource_record(
    report: GoldenReport,
    expected: GoldenResourceExpectation,
    tonnage: ResourceQuantity,
    scope: ResourceScope,
) -> ResourceRecord:
    return ResourceRecord(
        project=expected.project_fragment,
        deposit=expected.deposit_fragment,
        reporting_standard=report.reporting_standard,
        category=expected.category,
        report_date=expected.report_date,
        effective_date=expected.effective_date,
        scope=scope,
        tonnage=tonnage,
        grade=expected.grade,
        contained_materials=expected.contained_materials,
        cutoff=expected.cutoff,
        evidence=[
            PageEvidence(
                pdf_page=expected.source_pages[0], quote="Reviewed resource table"
            )
        ],
    )


@pytest.mark.parametrize(
    "report", GOLDEN.reports, ids=[report.filename for report in GOLDEN.reports]
)
def test_tonnage_material_must_match_the_resource_mass(report):
    expected = report.expectations[0]
    wrong = ResourceQuantity(
        value=expected.tonnage.value, unit=expected.tonnage.unit, material="Li2O"
    )
    with pytest.raises(ValidationError):
        resource_record(report, expected, wrong, expected.scope)
    correct = resource_record(report, expected, expected.tonnage, expected.scope)
    assert matches_record(correct, expected)


@pytest.mark.parametrize(
    "report", GOLDEN.reports, ids=[report.filename for report in GOLDEN.reports]
)
def test_resource_mass_labels_match_without_changing_source_material(report):
    expected = report.expectations[0]
    tonnage = ResourceQuantity(
        value=expected.tonnage.value,
        unit=expected.tonnage.unit,
        material="mineralized material",
    )
    record = resource_record(report, expected, tonnage, expected.scope)
    assert record.tonnage.material == "mineralized material"
    assert matches_record(record, expected)


@pytest.mark.parametrize(
    "scope", [ResourceScope.STOCKPILE, ResourceScope.TOTAL, ResourceScope.UNSPECIFIED]
)
def test_xuxa_block_model_is_not_a_stockpile_or_whole_project_total(scope):
    report = GOLDEN.reports[0]
    for expected in report.expectations:
        assert expected.scope == ResourceScope.IN_SITU
        assert not matches_record(
            resource_record(report, expected, expected.tonnage, scope), expected
        )
        assert matches_record(
            resource_record(report, expected, expected.tonnage, ResourceScope.IN_SITU),
            expected,
        )
