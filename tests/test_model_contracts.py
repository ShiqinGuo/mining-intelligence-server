import pytest
from pydantic import ValidationError

from mining_server.domain.auth import (
    Credentials,
    OAuthDiscovery,
    SecretSerialization,
    utcnow,
)
from mining_server.domain.documents import FinishExtractionArguments
from mining_server.domain.model import (
    JsonSchema,
    JsonSchemaType,
    ModelFunctionCall,
    ModelInputImage,
    ModelMessage,
    ModelRequest,
    ModelRole,
)
from mining_server.domain.news import ArticleAnalysisDraft


@pytest.mark.parametrize("contract", [ArticleAnalysisDraft, FinishExtractionArguments])
def test_concrete_json_schema_roundtrip_has_named_properties_and_definitions(contract):
    schema = JsonSchema.model_validate(contract.model_json_schema())
    assert all(property.definition is not None for property in schema.properties)
    decoded = JsonSchema.model_validate_json(schema.model_dump_json())
    assert decoded == schema
    assert schema.type == JsonSchemaType.OBJECT


def test_unknown_schema_field_and_invalid_input_discriminator_fail_at_boundary():
    with pytest.raises(ValidationError):
        JsonSchema.model_validate_json('{"type":"object","unimplementedKeyword":true}')
    with pytest.raises(ValidationError):
        ModelRequest.model_validate_json(
            '{"model":"example","input":[{"type":"message","role":"user","content":[{"type":"input_image","image_url":"image","detail":"invalid"}]}]}'
        )


def test_model_request_preserves_concrete_call_and_image_fields():
    request = ModelRequest(
        model="example",
        input=[
            ModelMessage(
                role=ModelRole.USER,
                content=[ModelInputImage(image_url="https://example.com/page.png")],
            ),
            ModelFunctionCall(call_id="call", name="read", arguments="{}"),
        ],
    )
    assert ModelRequest.model_validate_json(request.model_dump_json()) == request


def test_credentials_reveal_only_at_explicit_cipher_or_handoff_boundary():
    credentials = Credentials(
        subject="account",
        client_id="issued",
        access_token="private-access",
        refresh_token="private-refresh",
        id_token="private-identity",
        scopes=[
            "openid",
            "offline_access",
            "resource.invoke",
            "chatgpt.tokens.use.direct",
        ],
        expires_at=utcnow(),
    )
    assert "private-access" not in credentials.model_dump_json()
    assert "private-access" in credentials.model_dump_json(
        context=SecretSerialization.REVEAL
    )
    restored = Credentials.model_validate_json(
        credentials.model_dump_json(context=SecretSerialization.REVEAL)
    )
    assert restored.access_token.get_secret_value() == "private-access"


@pytest.mark.parametrize(
    "url",
    [
        "http://auth.openai.com/revoke",
        "https://attacker.example/revoke",
        "https://auth.openai.com@attacker.example/revoke",
    ],
)
def test_discovery_rejects_untrusted_revocation_endpoint(url):
    with pytest.raises(ValidationError):
        OAuthDiscovery(revocation_endpoint=url)
