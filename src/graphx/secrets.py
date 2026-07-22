"""Credential storage and late-resolution.

Secrets are referenced in workflows as ``secret://NAME`` — an opaque
string that flows through the loader, config, state, outputs, events,
checkpoints, and the TUI *unchanged*. The real value is substituted
only at the consumption points (api request, mcp/shell env, LLM
headers), immediately before use, so a value is never materialized into
anything durable or user-visible.

Storage: ``$GRAPHX_HOME/secrets.json`` (default ``~/.graphx``), created
0600. An OS keyring backend is used instead when the optional
``keyring`` package and a working backend are available.

Resolution order per name: secret store → process environment (so
``secret://GITHUB_TOKEN`` also finds an exported ``GITHUB_TOKEN``).
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any

SECRET_RE = re.compile(r"secret://([A-Za-z0-9_.\-]+)")
_KEYRING_SERVICE = "graphx"


def _home() -> Path:
    return Path(os.environ.get("GRAPHX_HOME", str(Path.home() / ".graphx")))


def secrets_path() -> Path:
    return _home() / "secrets.json"


def find_secret_refs(value: Any) -> set[str]:
    """Every ``secret://NAME`` name referenced anywhere in a config tree."""
    names: set[str] = set()
    if isinstance(value, str):
        names.update(SECRET_RE.findall(value))
    elif isinstance(value, dict):
        for v in value.values():
            names.update(find_secret_refs(v))
    elif isinstance(value, (list, tuple)):
        for v in value:
            names.update(find_secret_refs(v))
    return names


def _keyring():
    """Return the keyring module iff it has a usable (non-fail) backend."""
    try:
        import keyring
        from keyring.backends.fail import Keyring as FailKeyring
    except ImportError:
        return None
    try:
        if isinstance(keyring.get_keyring(), FailKeyring):
            return None
    except Exception:  # noqa: BLE001 — any backend probe failure => no keyring
        return None
    return keyring


class SecretStore:
    """File-backed secret store (0600), optional OS keyring."""

    def __init__(self, use_keyring: bool | None = None):
        # None => auto-detect; True/False => force
        self._keyring = _keyring() if use_keyring in (None, True) else None

    def backend(self) -> str:
        return "keyring" if self._keyring else "file"

    # ---- file backend helpers ----

    def _read_file(self) -> dict[str, str]:
        path = secrets_path()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text())
            return data if isinstance(data, dict) else {}
        except (ValueError, OSError):
            return {}

    def _write_file(self, data: dict[str, str]) -> None:
        path = secrets_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # write then chmod 0600 (create the file restrictively from the start)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, json.dumps(data, indent=2, sort_keys=True).encode())
        finally:
            os.close(fd)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)

    # ---- public API ----

    def get(self, name: str) -> str | None:
        if self._keyring:
            value = self._keyring.get_password(_KEYRING_SERVICE, name)
            if value is not None:
                return value
        else:
            value = self._read_file().get(name)
            if value is not None:
                return value
        return os.environ.get(name)  # env fallback

    def set(self, name: str, value: str) -> None:
        if not SECRET_RE.fullmatch(f"secret://{name}"):
            raise ValueError(f"invalid secret name {name!r} "
                             "(letters, digits, '_', '.', '-')")
        if self._keyring:
            self._keyring.set_password(_KEYRING_SERVICE, name, value)
        else:
            data = self._read_file()
            data[name] = value
            self._write_file(data)

    def delete(self, name: str) -> bool:
        if self._keyring:
            try:
                self._keyring.delete_password(_KEYRING_SERVICE, name)
                return True
            except Exception:  # noqa: BLE001 — not found / backend error
                return False
        data = self._read_file()
        if name in data:
            del data[name]
            self._write_file(data)
            return True
        return False

    def names(self) -> list[str]:
        """Stored names (keyring can't enumerate — file backend only)."""
        if self._keyring:
            return []  # keyring offers no portable enumeration
        return sorted(self._read_file())


class SecretResolver:
    """Substitutes ``secret://NAME`` with real values at consumption time,
    recording every value handed out so the Redactor can mask them."""

    def __init__(self, store: SecretStore | None = None):
        self.store = store or SecretStore()
        self.used_values: set[str] = set()

    def _lookup(self, name: str) -> str | None:
        value = self.store.get(name)
        if value:
            self.used_values.add(value)
        return value

    def resolve(self, value: Any, *, strict: bool = True) -> Any:
        """Return a copy of value with all secret:// refs substituted.

        strict=True raises on a missing secret; strict=False leaves the
        ``secret://NAME`` placeholder in place (used where a missing
        secret should surface later, not crash resolution)."""
        if isinstance(value, str):
            def sub(match: re.Match[str]) -> str:
                name = match.group(1)
                resolved = self._lookup(name)
                if resolved is None:
                    if strict:
                        raise KeyError(
                            f"secret '{name}' is not set "
                            f"(run: graphx secret set {name})")
                    return match.group(0)
                return resolved
            return SECRET_RE.sub(sub, value)
        if isinstance(value, dict):
            return {k: self.resolve(v, strict=strict) for k, v in value.items()}
        if isinstance(value, list):
            return [self.resolve(v, strict=strict) for v in value]
        if isinstance(value, tuple):
            return tuple(self.resolve(v, strict=strict) for v in value)
        return value

    def missing(self, names: set[str] | list[str]) -> list[str]:
        return sorted(n for n in names if self.store.get(n) is None)


class Redactor:
    """Masks known secret values in any object about to be serialized or
    shown. Holds a live reference to a resolver's ``used_values`` so it
    only masks values actually handed out — no false positives."""

    def __init__(self, values: set[str], placeholder: str = "***"):
        self._values = values
        self.placeholder = placeholder

    def redact(self, obj: Any) -> Any:
        if not self._values:
            return obj
        if isinstance(obj, str):
            out = obj
            for value in self._values:
                if value and value in out:
                    out = out.replace(value, self.placeholder)
            return out
        if isinstance(obj, dict):
            return {k: self.redact(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.redact(v) for v in obj]
        if isinstance(obj, tuple):
            return tuple(self.redact(v) for v in obj)
        return obj


_NULL_RESOLVER = SecretResolver.__new__(SecretResolver)


def null_resolver() -> SecretResolver:
    """A resolver with no store lookups — leaves placeholders untouched.
    Used as the default in Services so nodes never crash when no secret
    layer is wired (e.g. isolated unit tests)."""
    resolver = SecretResolver.__new__(SecretResolver)
    resolver.store = None  # type: ignore[assignment]
    resolver.used_values = set()
    resolver.resolve = lambda value, *, strict=True: value  # type: ignore[assignment]
    resolver.missing = lambda names: []  # type: ignore[assignment]
    return resolver
