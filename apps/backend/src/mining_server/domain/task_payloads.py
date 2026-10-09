from mining_contracts.domain.tasks import TaskKind

from mining_server.domain.documents import DocumentIngestPayload, ExtractionPayload
from mining_server.domain.market import MarketRefreshPayload
from mining_server.domain.news import (
    AnalysisPayload,
    ArticleFetchPayload,
    CollectionPayload,
)

type TaskPayload = (
    DocumentIngestPayload
    | ExtractionPayload
    | MarketRefreshPayload
    | AnalysisPayload
    | ArticleFetchPayload
    | CollectionPayload
)


def parse_task_payload(kind: TaskKind, encoded: str) -> TaskPayload:
    match kind:
        case TaskKind.DOCUMENT_INGEST:
            return DocumentIngestPayload.model_validate_json(encoded)
        case TaskKind.RESOURCE_EXTRACTION:
            return ExtractionPayload.model_validate_json(encoded)
        case TaskKind.COLLECT_SOURCE:
            return CollectionPayload.model_validate_json(encoded)
        case TaskKind.FETCH_ARTICLE:
            return ArticleFetchPayload.model_validate_json(encoded)
        case TaskKind.COLLECT_PRICES:
            return MarketRefreshPayload.model_validate_json(encoded)
        case TaskKind.NEWS_ANALYSIS:
            return AnalysisPayload.model_validate_json(encoded)
