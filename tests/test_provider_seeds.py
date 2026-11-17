"""Built-in provider seeds: sourced, backed by an adapter, and honest about data use."""

from __future__ import annotations

from oasis_route.adapters.openai_compat import OpenAICompatibleAdapter
from oasis_route.adapters.registry import PROFILES, AdapterRegistry
from oasis_route.registry.catalog import PROVIDERS, get_seed


def test_new_seeds_present_with_terms_and_https() -> None:
    for pid in ("nvidia", "sambanova", "huggingface"):
        seed = get_seed(pid)
        assert seed is not None, pid
        assert seed.terms_url and seed.terms_url.startswith("https://")
        assert seed.base_url.startswith("https://") and seed.base_url.endswith("/v1")
        assert pid in PROFILES


def test_every_seed_has_an_adapter() -> None:
    reg = AdapterRegistry()
    for seed in PROVIDERS:
        assert reg.for_provider(seed.id, seed.adapter_key) is not None


def test_nvidia_records_training_use_with_source() -> None:
    seed = get_seed("nvidia")
    assert seed is not None
    assert seed.data_policy["trains_on_inputs"] is True
    assert seed.data_policy["source"].startswith("https://build.nvidia.com/")


def test_mixed_catalogues_do_not_overclaim_capabilities() -> None:
    for pid in ("nvidia", "sambanova", "huggingface"):
        adapter = AdapterRegistry().for_provider(pid, "openai_compatible")
        assert isinstance(adapter, OpenAICompatibleAdapter)
        caps = adapter.profile.default_capabilities
        assert caps == {"streaming": True}
