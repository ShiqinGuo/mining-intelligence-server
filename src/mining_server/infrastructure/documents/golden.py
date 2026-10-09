from mining_server.domain.documents import (
    GoldenReport,
    GoldenResourceExpectation,
    GoldenVerification,
    ResourceCategory,
    ResourceExtractionResult,
    ResourceQuantity,
    ResourceRecord,
    ResourceScope,
)


def contains_quantities(
    actual: list[ResourceQuantity], expected: list[ResourceQuantity]
) -> bool:
    return all(
        any(
            item.value == value.value
            and item.unit == value.unit
            and item.material.casefold().replace(" ", "")
            == value.material.casefold().replace(" ", "")
            for item in actual
        )
        for value in expected
    )


def matches_record(record: ResourceRecord, expected: GoldenResourceExpectation) -> bool:
    if expected.project_fragment.casefold() not in record.project.casefold():
        return False
    if (
        expected.deposit_fragment
        and expected.deposit_fragment.casefold()
        not in (record.deposit or "").casefold()
    ):
        return False
    if (
        record.category != expected.category
        or record.report_date != expected.report_date
        or record.effective_date != expected.effective_date
    ):
        return False
    if expected.scope is not None and record.scope != expected.scope:
        return False
    if record.tonnage is None:
        return False
    if (
        record.tonnage.value != expected.tonnage.value
        or record.tonnage.unit != expected.tonnage.unit
    ):
        return False
    if (
        not contains_quantities(record.grade, expected.grade)
        or not contains_quantities(
            record.contained_materials, expected.contained_materials
        )
        or not contains_quantities(record.cutoff, expected.cutoff)
    ):
        return False
    return any(
        evidence.pdf_page in expected.source_pages for evidence in record.evidence
    )


def verify_golden(
    result: ResourceExtractionResult, report: GoldenReport
) -> GoldenVerification:
    failures: list[str] = []
    matched = 0
    if result.document_sha256 != report.sha256:
        failures.append("Document hash differs from the independently reviewed report")
    if result.reporting_standard != report.reporting_standard:
        failures.append("Reporting standard differs from the report")
    if not result.complete:
        failures.append("Extraction is incomplete")
    for expected in report.expectations:
        records = [
            record
            for record in result.records
            if record.reporting_standard == report.reporting_standard
            and matches_record(record, expected)
        ]
        if records:
            matched += 1
        else:
            failures.append(
                f"Missing exact golden record: {expected.deposit_fragment or expected.project_fragment}, {expected.scope}, {expected.category}, {expected.tonnage.value} {expected.tonnage.unit}, effective {expected.effective_date}"
            )
    if report.absent_stockpile_inferred and any(
        record.scope == ResourceScope.STOCKPILE
        and record.category == ResourceCategory.INFERRED
        and (record.tonnage is not None or record.grade or record.contained_materials)
        for record in result.records
    ):
        failures.append(
            "Dash-only inferred stockpile row was fabricated as numeric resources"
        )
    return GoldenVerification(
        filename=report.filename,
        passed=not failures,
        failures=failures,
        matched_records=matched,
    )
