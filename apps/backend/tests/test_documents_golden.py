import hashlib
import os
from pathlib import Path
from uuid import uuid4

import pytest
from mining_contracts.domain.documents import (
    PageEvidence,
    QuantityUnit,
    ResourceCategory,
    ResourceExtractionResult,
    ResourceQuantity,
    ResourceRecord,
    ResourceScope,
)

from mining_server.domain.documents import FinishExtractionArguments, GoldenReports
from mining_server.infrastructure.documents.agent import (
    DocumentAgent,
    InMemoryDocumentPages,
)
from mining_server.infrastructure.documents.golden import verify_golden
from mining_server.infrastructure.documents.parser import PdfParser

GOLDEN = GoldenReports.model_validate_json(
    (Path(__file__).parent / "fixtures/document-golden.json").read_text(
        encoding="utf-8"
    )
)


@pytest.mark.parametrize(
    "report", GOLDEN.reports, ids=[report.filename for report in GOLDEN.reports]
)
async def test_reviewed_golden_values_retain_exact_page_evidence(report):
    fixture_dir = None
    if "MINING_REPORT_FIXTURE_DIR" in os.environ:
        fixture_dir = os.environ["MINING_REPORT_FIXTURE_DIR"]
    if fixture_dir is None:
        pytest.skip("Download golden reports and set MINING_REPORT_FIXTURE_DIR")
    path = Path(fixture_dir) / report.filename
    assert hashlib.sha256(path.read_bytes()).hexdigest() == report.sha256
    parsed = await PdfParser().parse(path)
    assert len(parsed.pages) == report.page_count
    records = [
        ResourceRecord(
            project=expected.project_fragment,
            deposit=expected.deposit_fragment,
            reporting_standard=report.reporting_standard,
            category=expected.category,
            scope=expected.scope or ResourceScope.UNSPECIFIED,
            report_date=expected.report_date,
            effective_date=expected.effective_date,
            tonnage=expected.tonnage,
            grade=expected.grade,
            contained_materials=expected.contained_materials,
            cutoff=expected.cutoff,
            evidence=[
                PageEvidence(
                    pdf_page=expected.source_pages[0],
                    quote=parsed.pages[expected.source_pages[0] - 1].text,
                )
            ],
        )
        for expected in report.expectations
    ]
    extraction = FinishExtractionArguments(
        reporting_standard=report.reporting_standard, records=records, complete=True
    )
    await DocumentAgent(None, PdfParser(), "gpt-6.1-sol").verify(
        extraction, InMemoryDocumentPages(parsed), report.reporting_standard, set()
    )
    result = ResourceExtractionResult(
        document_id=uuid4(),
        document_sha256=report.sha256,
        reporting_standard=report.reporting_standard,
        records=records,
        complete=True,
    )
    verification = verify_golden(result, report)
    assert verification.passed, verification.failures


def test_golden_rejects_falchani_historical_estimate_as_current():
    report = GOLDEN.reports[1]
    expected = report.expectations[0]
    record = ResourceRecord(
        project=expected.project_fragment,
        reporting_standard=report.reporting_standard,
        category=expected.category,
        scope=expected.scope,
        report_date=expected.report_date,
        effective_date=expected.effective_date,
        tonnage=ResourceQuantity(
            value="60.92",
            unit=expected.tonnage.unit,
            material=expected.tonnage.material,
        ),
        grade=expected.grade,
        contained_materials=expected.contained_materials,
        cutoff=expected.cutoff,
        evidence=[PageEvidence(pdf_page=11, quote="2019 historical estimates")],
    )
    result = ResourceExtractionResult(
        document_id=uuid4(),
        document_sha256=report.sha256,
        reporting_standard=report.reporting_standard,
        records=[record],
        complete=True,
    )
    assert not verify_golden(result, report).passed


def test_golden_rejects_volume_column_as_tonnage():
    report = GOLDEN.reports[1]
    expected = next(
        item
        for item in report.expectations
        if item.category == ResourceCategory.INFERRED
    )
    assert expected.tonnage.value == 506
    records = [
        ResourceRecord(
            project=item.project_fragment,
            reporting_standard=report.reporting_standard,
            category=item.category,
            scope=item.scope,
            report_date=item.report_date,
            effective_date=item.effective_date,
            tonnage=item.tonnage,
            grade=item.grade,
            contained_materials=item.contained_materials,
            cutoff=item.cutoff,
            evidence=[PageEvidence(pdf_page=13, quote="Reviewed Table 1.4 at 600 ppm")],
        )
        for item in report.expectations
    ]
    result = ResourceExtractionResult(
        document_id=uuid4(),
        document_sha256=report.sha256,
        reporting_standard=report.reporting_standard,
        records=records,
        complete=True,
    )
    assert verify_golden(result, report).passed
    original = records[1]
    wrong = ResourceRecord(
        project=original.project,
        reporting_standard=original.reporting_standard,
        category=original.category,
        scope=original.scope,
        report_date=original.report_date,
        effective_date=original.effective_date,
        tonnage=ResourceQuantity(
            value=198, unit=QuantityUnit.MEGATONNE, material="ore"
        ),
        grade=original.grade,
        contained_materials=original.contained_materials,
        cutoff=original.cutoff,
        evidence=original.evidence,
    )
    failed = verify_golden(
        ResourceExtractionResult(
            document_id=result.document_id,
            document_sha256=result.document_sha256,
            reporting_standard=result.reporting_standard,
            records=[records[0], wrong],
            complete=result.complete,
        ),
        report,
    )
    assert not failed.passed
    assert failed.matched_records == 1
    assert any("506" in message for message in failed.failures)
