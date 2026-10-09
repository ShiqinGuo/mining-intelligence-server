import httpx

from mining_server.domain.model import ModelTransportFailure, ModelTransportLimits


def model_timeout(limits: ModelTransportLimits) -> httpx.Timeout:
    return httpx.Timeout(
        connect=limits.connect_timeout_seconds,
        read=limits.read_timeout_seconds,
        write=limits.write_timeout_seconds,
        pool=limits.pool_timeout_seconds,
    )


def transport_failure(error: httpx.TransportError) -> ModelTransportFailure:
    match error:
        case httpx.ConnectTimeout():
            return ModelTransportFailure.CONNECT_TIMEOUT
        case httpx.ReadTimeout():
            return ModelTransportFailure.READ_TIMEOUT
        case httpx.WriteTimeout():
            return ModelTransportFailure.WRITE_TIMEOUT
        case httpx.PoolTimeout():
            return ModelTransportFailure.POOL_TIMEOUT
        case httpx.ConnectError():
            return ModelTransportFailure.CONNECT_ERROR
        case httpx.ReadError():
            return ModelTransportFailure.READ_ERROR
        case httpx.WriteError():
            return ModelTransportFailure.WRITE_ERROR
        case httpx.CloseError():
            return ModelTransportFailure.CLOSE_ERROR
        case httpx.LocalProtocolError():
            return ModelTransportFailure.LOCAL_PROTOCOL_ERROR
        case httpx.RemoteProtocolError():
            return ModelTransportFailure.REMOTE_PROTOCOL_ERROR
        case httpx.ProxyError():
            return ModelTransportFailure.PROXY_ERROR
        case httpx.UnsupportedProtocol():
            return ModelTransportFailure.UNSUPPORTED_PROTOCOL
        case httpx.TransportError():
            return ModelTransportFailure.TRANSPORT_ERROR
