from bs4 import Tag
from mining_contracts.domain.core import ErrorCode
from pydantic import ValidationError

from mining_server.domain.core import fail
from mining_server.domain.news import HtmlElementAttributes


def element_attributes(node: Tag) -> HtmlElementAttributes:
    try:
        return HtmlElementAttributes.model_validate(node.attrs)
    except ValidationError as error:
        raise fail(
            ErrorCode.UPSTREAM_FAILURE, "HTML attributes have invalid field types"
        ) from error
