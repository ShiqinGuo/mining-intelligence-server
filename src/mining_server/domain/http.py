from enum import StrEnum


class HttpScheme(StrEnum):
    HTTP = "http"
    HTTPS = "https"


class HttpMethod(StrEnum):
    GET = "GET"
    POST = "POST"


class HttpHeader(StrEnum):
    AUTHORIZATION = "authorization"
    REQUEST_ID = "X-Request-ID"
    IDEMPOTENCY_KEY = "Idempotency-Key"
    CONTENT_TYPE = "Content-Type"


class MediaType(StrEnum):
    JSON = "application/json"


class BackendPath(StrEnum):
    NEWS = "/api/v1/news"
    ARTICLE_FETCH = "/api/v1/articles/fetch"
    TASKS = "/api/v1/tasks"
    INSTRUMENTS = "/api/v1/price-instruments"
    PRICES = "/api/v1/prices"
    TRENDS = "/api/v1/price-trends"
    EXTRACTIONS = "/api/v1/resource-extractions"
    LIVE = "/health/live"


class QueryField(StrEnum):
    QUERY = "query"
    DAYS = "days"
    COMMODITY = "commodity"
    DATE = "date"
    MODE = "mode"
