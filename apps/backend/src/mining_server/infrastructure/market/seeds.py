from decimal import Decimal

from mining_contracts.domain.market import (
    Commodity,
    Currency,
    DeliveryBasis,
    InstrumentCreate,
    MysteelIndex,
    PriceAdapter,
    PriceFrequency,
    PriceKind,
    PriceSession,
    PriceUnit,
    TaxBasis,
)


def defaults() -> list[InstrumentCreate]:
    notice = "Publicly readable source for authenticated personal deployment; commercial use and redistribution permission are not established"
    instruments = []
    for slug, name, grade, purity, delivery, session, symbol in [
        (
            "lithium_carbonate_battery_99_5_china_am",
            "Mysteel battery lithium carbonate morning",
            "battery",
            "99.5",
            DeliveryBasis.DELIVERED,
            PriceSession.MORNING,
            MysteelIndex.BATTERY_MORNING,
        ),
        (
            "lithium_carbonate_technical_99_2_china_am",
            "Mysteel technical lithium carbonate morning",
            "technical",
            "99.2",
            DeliveryBasis.PICKUP,
            PriceSession.MORNING,
            MysteelIndex.TECHNICAL_MORNING,
        ),
        (
            "lithium_carbonate_battery_99_5_china_pm",
            "Mysteel battery lithium carbonate afternoon",
            "battery",
            "99.5",
            DeliveryBasis.DELIVERED,
            PriceSession.AFTERNOON,
            MysteelIndex.BATTERY_AFTERNOON,
        ),
    ]:
        instruments.append(
            InstrumentCreate(
                slug=slug,
                name=name,
                commodity=Commodity.LITHIUM_CARBONATE,
                grade=grade,
                purity_basis="Li2CO3",
                purity_min=Decimal(purity),
                region="China",
                price_kind=PriceKind.SPOT_ASSESSMENT,
                frequency=PriceFrequency.TRADING_DAY,
                currency=Currency.CNY,
                unit=PriceUnit.TONNE,
                tax_basis=TaxBasis.VAT_13_INCLUDED,
                delivery_basis=delivery,
                session=session,
                adapter=PriceAdapter.MYSTEEL_JSON,
                source_symbol=symbol,
                source_url="https://www.mysteel.com/mmlc/",
                methodology_url="https://a.mysteelcdn.com/common/mysteel/dataIndex/tansuanli/file/tsl-methodology.pdf?v=20260421",
                methodology_version="V2.2 2026-04-07",
                usage_notice=notice,
            )
        )
    instruments.append(
        InstrumentCreate(
            slug="lithium_carbonate_futures_lc2701",
            name="Sina GFEX lithium carbonate LC2701 daily close",
            commodity=Commodity.LITHIUM_CARBONATE,
            grade="exchange contract LC2701",
            purity_basis="exchange delivery standard",
            purity_min=Decimal("99.5"),
            region="GFEX China",
            price_kind=PriceKind.FUTURES_CONTRACT,
            frequency=PriceFrequency.TRADING_DAY,
            currency=Currency.CNY,
            unit=PriceUnit.TONNE,
            tax_basis=TaxBasis.NOT_APPLICABLE,
            delivery_basis=DeliveryBasis.EXCHANGE,
            session=PriceSession.DAILY,
            adapter=PriceAdapter.SINA_DAILY,
            source_symbol="LC2701",
            source_url="https://finance.sina.com.cn/futures/quotes/gfex/LC2701.shtml",
            usage_notice=notice,
        )
    )
    instruments.append(
        InstrumentCreate(
            slug="spodumene_sc6_australia_cif_china_public_articles",
            name="Mysteel public article Australia SC6 forward physical spot",
            commodity=Commodity.SPODUMENE,
            grade="SC6",
            purity_basis="Li2O",
            purity_min=Decimal(6),
            region="China",
            origin="Australia",
            price_kind=PriceKind.FORWARD_PHYSICAL_SPOT,
            frequency=PriceFrequency.SPARSE,
            currency=Currency.USD,
            unit=PriceUnit.TONNE,
            tax_basis=TaxBasis.UNKNOWN,
            delivery_basis=DeliveryBasis.CIF,
            session=PriceSession.DAILY,
            adapter=PriceAdapter.MYSTEEL_ARTICLE,
            source_symbol="AU_SC6_PUBLIC_ARTICLES",
            source_url="https://xny.mysteel.com/",
            usage_notice=notice
            + "; sparse article observations, not continuous daily history",
        )
    )
    instruments.append(
        InstrumentCreate(
            slug="pls_pilgangoora_sc6_equivalent_quarterly",
            enabled=False,
            name="PLS Pilgangoora estimated realised SC6-equivalent quarter price",
            commodity=Commodity.SPODUMENE,
            grade="issuer SC6 equivalent",
            purity_basis="Li2O equivalent",
            purity_min=Decimal(6),
            region="China",
            origin="Australia",
            issuer="PLS",
            project="Pilgangoora",
            price_kind=PriceKind.ISSUER_REALIZED,
            frequency=PriceFrequency.QUARTERLY,
            currency=Currency.USD,
            unit=PriceUnit.TONNE,
            tax_basis=TaxBasis.UNKNOWN,
            delivery_basis=DeliveryBasis.CIF,
            session=PriceSession.PERIOD,
            adapter=PriceAdapter.PLS_REPORT,
            source_symbol="PLS_SC6_EQUIVALENT",
            source_url="https://announcements.asx.com.au/asxpdf/20251024/pdf/06qz382w4nt85r.pdf",
            usage_notice="Public issuer disclosure; estimated quarter realised price, not daily spot; original product grades vary",
        )
    )
    return instruments
