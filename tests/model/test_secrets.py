"""Secret store, resolution, and redaction unit tests."""

import os
import stat

import pytest

from graphx.secrets import (
    Redactor, SecretResolver, SecretStore, find_secret_refs, secrets_path,
)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAPHX_HOME", str(tmp_path))
    # force the file backend so tests never touch a real OS keyring
    monkeypatch.setattr("graphx.secrets._keyring", lambda: None)


class TestStore:
    def test_set_get_roundtrip(self):
        store = SecretStore()
        store.set("api_key", "sk-123")
        assert store.get("api_key") == "sk-123"
        assert store.backend() == "file"

    def test_file_is_0600(self):
        SecretStore().set("k", "v")
        mode = stat.S_IMODE(os.stat(secrets_path()).st_mode)
        assert mode == 0o600

    def test_list_names_not_values(self):
        store = SecretStore()
        store.set("a", "secret-a")
        store.set("b", "secret-b")
        assert store.names() == ["a", "b"]

    def test_delete(self):
        store = SecretStore()
        store.set("x", "1")
        assert store.delete("x") is True
        assert store.get("x") is None
        assert store.delete("x") is False

    def test_env_fallback(self, monkeypatch):
        monkeypatch.setenv("FROM_ENV", "envval")
        assert SecretStore().get("FROM_ENV") == "envval"

    def test_store_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("BOTH", "envval")
        store = SecretStore()
        store.set("BOTH", "storeval")
        assert store.get("BOTH") == "storeval"

    def test_invalid_name_rejected(self):
        with pytest.raises(ValueError):
            SecretStore().set("bad name!", "v")


class TestFindRefs:
    def test_nested(self):
        config = {
            "url": "https://api/x?k=secret://api_key",
            "headers": {"Authorization": "Bearer secret://token"},
            "list": ["secret://a", {"deep": "secret://b"}],
            "plain": "no refs here",
        }
        assert find_secret_refs(config) == {"api_key", "token", "a", "b"}

    def test_none_when_absent(self):
        assert find_secret_refs({"a": "hello", "b": ["x"]}) == set()


class TestResolver:
    def test_whole_string_and_embedded(self):
        store = SecretStore()
        store.set("k", "VALUE")
        r = SecretResolver(store)
        assert r.resolve("secret://k") == "VALUE"
        assert r.resolve("Bearer secret://k") == "Bearer VALUE"

    def test_nested_structure(self):
        store = SecretStore()
        store.set("k", "V")
        r = SecretResolver(store)
        out = r.resolve({"h": {"Auth": "secret://k"}, "l": ["secret://k"]})
        assert out == {"h": {"Auth": "V"}, "l": ["V"]}

    def test_records_used_values(self):
        store = SecretStore()
        store.set("k", "topsecret")
        r = SecretResolver(store)
        r.resolve("secret://k")
        assert "topsecret" in r.used_values

    def test_strict_raises_on_missing(self):
        r = SecretResolver(SecretStore())
        with pytest.raises(KeyError):
            r.resolve("secret://nope", strict=True)

    def test_lenient_leaves_placeholder(self):
        r = SecretResolver(SecretStore())
        assert r.resolve("secret://nope", strict=False) == "secret://nope"

    def test_missing(self):
        store = SecretStore()
        store.set("have", "1")
        r = SecretResolver(store)
        assert r.missing({"have", "missing1", "missing2"}) == ["missing1", "missing2"]


class TestRedactor:
    def test_masks_known_values_nested(self):
        r = Redactor({"topsecret", "key2"})
        obj = {"a": "prefix-topsecret-suffix", "b": ["has key2 inside"], "c": 5}
        out = r.redact(obj)
        assert out == {"a": "prefix-***-suffix", "b": ["has *** inside"], "c": 5}

    def test_no_false_positives(self):
        r = Redactor({"topsecret"})
        assert r.redact("nothing sensitive here") == "nothing sensitive here"

    def test_empty_value_set_is_noop(self):
        r = Redactor(set())
        obj = {"a": "anything"}
        assert r.redact(obj) is obj

    def test_live_view_of_resolver_values(self):
        store = SecretStore()
        store.set("k", "livesecret")
        resolver = SecretResolver(store)
        redactor = Redactor(resolver.used_values)     # live reference
        # nothing used yet -> no masking
        assert redactor.redact("livesecret x") == "livesecret x"
        resolver.resolve("secret://k")                # now used
        assert redactor.redact("livesecret x") == "*** x"
