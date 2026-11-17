"""Capabilities stated in provider /models listings are picked up."""

from oasis_route.adapters.openai_compat import infer_capabilities


def test_openrouter_supported_parameters_and_modalities() -> None:
    caps, max_out = infer_capabilities(
        {
            "id": "meta-llama/llama-3.3-70b-instruct:free",
            "supported_parameters": ["max_tokens", "temperature", "tools", "tool_choice", "response_format"],
            "architecture": {"input_modalities": ["text", "image"]},
            "top_provider": {"max_completion_tokens": 8192},
        }
    )
    assert caps == {"tools": True, "json_mode": True, "vision": True}
    assert max_out == 8192


def test_openrouter_without_tools_is_stated_false() -> None:
    caps, _ = infer_capabilities({"id": "x", "supported_parameters": ["max_tokens", "temperature"]})
    assert caps["tools"] is False
    assert caps["json_mode"] is False


def test_mistral_capabilities_object() -> None:
    caps, _ = infer_capabilities(
        {
            "id": "mistral-small",
            "capabilities": {"completion_chat": True, "function_calling": True, "vision": False},
        }
    )
    assert caps == {"tools": True, "vision": False}


def test_mistral_embedding_model_is_not_chat() -> None:
    caps, _ = infer_capabilities({"id": "mistral-embed", "capabilities": {"completion_chat": False}})
    assert caps["chat"] is False


def test_pollinations_top_level_flags() -> None:
    caps, _ = infer_capabilities(
        {"id": "openai-fast", "tools": True, "vision": True, "input_modalities": ["text", "image"]}
    )
    assert caps["tools"] is True
    assert caps["vision"] is True


def test_listing_without_capability_data_states_nothing() -> None:
    assert infer_capabilities({"id": "plain", "object": "model", "owned_by": "x"}) == ({}, None)
