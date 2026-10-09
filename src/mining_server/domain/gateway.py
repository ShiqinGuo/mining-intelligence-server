from enum import StrEnum

from mining_server.domain.core import Contract


class ServerName(StrEnum):
    NEWS = "mining-news"
    DOCUMENTS = "mining-documents"
    MARKET = "mining-market"


class GatewayStatus(StrEnum):
    OK = "ok"


class GatewayHealth(Contract):
    status: GatewayStatus = GatewayStatus.OK
    servers: list[ServerName]


class MCPContentType(StrEnum):
    TEXT = "text"
