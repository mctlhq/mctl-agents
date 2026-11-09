"""RFC 8785 (JCS) canonicalizer and raw-bytes pre-check for the bridge v1alpha2 contract.

Stdlib only. Scoped to the bridge contract (docs/contracts/bridge/v1alpha2/):
this is NOT orchestrator.context_snapshot.canonical_json, which is a different,
non-JCS canonical form and must not change.

Functions:
    precheck(raw)             token-level scan of raw JSON bytes, rejects parser
                              differentials, returns every number token
    canonicalize(value)       JCS bytes of a parsed value
    canonicalize_text(text)   JCS bytes of JSON text (duplicate keys rejected)
    digest(value)             "sha256:" + hex(SHA-256(JCS(value)))
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from decimal import Decimal
from typing import Any

MAX_SAFE_INT = 2**53 - 1
MAX_DEPTH = 32

# A JSON integer token (no fraction, no exponent).
INTEGER_TOKEN = re.compile(r"-?(0|[1-9][0-9]*)")
_NUMBER_TOKEN = re.compile(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?")
_WS = " \t\n\r"
_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


class JCSError(ValueError):
    """A rejected document. `code` is a stable machine-readable reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- precheck


class _Scanner:
    def __init__(self, text: str) -> None:
        self.s = text
        self.i = 0
        self.numbers: list[tuple[tuple[str | int, ...], str]] = []

    def fail(self, code: str, message: str) -> JCSError:
        return JCSError(code, f"{message} at offset {self.i}")

    def ws(self) -> None:
        while self.i < len(self.s) and self.s[self.i] in _WS:
            self.i += 1

    def document(self) -> None:
        self.ws()
        if self.i >= len(self.s) or self.s[self.i] != "{":
            raise self.fail("not_object", "the root must be exactly one JSON object")
        self.value((), 0)
        self.ws()
        if self.i != len(self.s):
            raise self.fail("trailing_bytes", "bytes after the root object")

    def value(self, path: tuple[str | int, ...], depth: int) -> None:
        if depth > MAX_DEPTH:
            raise self.fail("too_deep", f"nesting deeper than {MAX_DEPTH}")
        if self.i >= len(self.s):
            raise self.fail("syntax", "unexpected end of input")
        c = self.s[self.i]
        if c == "{":
            self.obj(path, depth)
        elif c == "[":
            self.arr(path, depth)
        elif c == '"':
            self.string()
        elif c == "-" or (c.isascii() and c.isdigit()):
            self.number(path)
        else:
            for lit in ("true", "false", "null"):
                if self.s.startswith(lit, self.i):
                    self.i += len(lit)
                    return
            if (
                self.s.startswith("NaN", self.i)
                or self.s.startswith("Infinity", self.i)
                or self.s.startswith("-Infinity", self.i)
            ):
                raise self.fail("non_finite", "NaN and Infinity are not allowed")
            raise self.fail("syntax", f"unexpected character {c!r}")

    def number(self, path: tuple[str | int, ...]) -> None:
        if self.s.startswith("-Infinity", self.i):
            raise self.fail("non_finite", "NaN and Infinity are not allowed")
        m = _NUMBER_TOKEN.match(self.s, self.i)
        if not m:
            raise self.fail("syntax", "malformed number")
        token = m.group(0)
        if INTEGER_TOKEN.fullmatch(token):
            if abs(int(token)) > MAX_SAFE_INT:
                raise self.fail("number_range", f"integer {token} is outside +-(2^53-1)")
        else:
            value = float(token)
            if not math.isfinite(value):
                raise self.fail("number_range", f"number {token} is not finite")
            if abs(value) > MAX_SAFE_INT:
                raise self.fail("number_range", f"number {token} is outside +-(2^53-1)")
        self.i = m.end()
        self.numbers.append((path, token))

    def string(self) -> tuple[str, bool]:
        """Parse a string. Returns (unescaped text, contained a \\u escape)."""
        self.i += 1  # opening quote
        out: list[str] = []
        had_u = False
        s = self.s
        while True:
            if self.i >= len(s):
                raise self.fail("syntax", "unterminated string")
            c = s[self.i]
            if c == '"':
                self.i += 1
                return "".join(out), had_u
            if ord(c) < 0x20:
                raise self.fail("control_char", "raw control character in a string")
            if 0xD800 <= ord(c) <= 0xDFFF:
                raise self.fail("lone_surrogate", "surrogate code point in a string")
            if c != "\\":
                out.append(c)
                self.i += 1
                continue
            self.i += 1
            if self.i >= len(s):
                raise self.fail("syntax", "unterminated escape")
            e = s[self.i]
            if e in _ESCAPES:
                out.append(_ESCAPES[e])
                self.i += 1
            elif e == "u":
                had_u = True
                unit = self.hex4(self.i + 1)
                self.i += 5
                if 0xD800 <= unit <= 0xDBFF:
                    if s.startswith("\\u", self.i):
                        low = self.hex4(self.i + 2)
                        if 0xDC00 <= low <= 0xDFFF:
                            self.i += 6
                            out.append(chr(0x10000 + ((unit - 0xD800) << 10) + (low - 0xDC00)))
                            continue
                    raise self.fail("lone_surrogate", "lone high surrogate escape")
                if 0xDC00 <= unit <= 0xDFFF:
                    raise self.fail("lone_surrogate", "lone low surrogate escape")
                out.append(chr(unit))
            else:
                raise self.fail("syntax", f"invalid escape \\{e}")

    def hex4(self, at: int) -> int:
        digits = self.s[at : at + 4]
        if len(digits) != 4 or not all(ch in "0123456789abcdefABCDEF" for ch in digits):
            raise self.fail("syntax", "malformed \\u escape")
        return int(digits, 16)

    def obj(self, path: tuple[str | int, ...], depth: int) -> None:
        self.i += 1
        seen: set[str] = set()
        self.ws()
        if self.s.startswith("}", self.i):
            self.i += 1
            return
        while True:
            self.ws()
            if self.i >= len(self.s) or self.s[self.i] != '"':
                raise self.fail("syntax", "object key must be a string")
            key, had_u = self.string()
            # Duplicates are compared after unescaping: "a" and "\u0061" are one key.
            if key in seen:
                raise self.fail("duplicate_key", f"duplicate key {key!r}")
            seen.add(key)
            if had_u:
                raise self.fail("escaped_key", f"\\u escape inside the object key {key!r}")
            self.ws()
            if not self.s.startswith(":", self.i):
                raise self.fail("syntax", "expected ':'")
            self.i += 1
            self.ws()
            self.value((*path, key), depth + 1)
            self.ws()
            if self.s.startswith(",", self.i):
                self.i += 1
                continue
            if self.s.startswith("}", self.i):
                self.i += 1
                return
            raise self.fail("syntax", "expected ',' or '}'")

    def arr(self, path: tuple[str | int, ...], depth: int) -> None:
        self.i += 1
        self.ws()
        if self.s.startswith("]", self.i):
            self.i += 1
            return
        index = 0
        while True:
            self.ws()
            self.value((*path, index), depth + 1)
            index += 1
            self.ws()
            if self.s.startswith(",", self.i):
                self.i += 1
                continue
            if self.s.startswith("]", self.i):
                self.i += 1
                return
            raise self.fail("syntax", "expected ',' or ']'")


def precheck(raw: bytes) -> list[tuple[tuple[str | int, ...], str]]:
    """Scan raw JSON bytes; raise JCSError on any parser differential.

    Rejects: invalid UTF-8, a BOM, a root that is not exactly one object, trailing
    bytes, raw control characters in strings, nesting deeper than MAX_DEPTH,
    duplicate keys at any depth (compared after unescaping), any \\u escape inside
    an object key, lone surrogate escapes, NaN/Infinity, and numbers outside
    +-(2^53-1) (an I-JSON profile rule on top of RFC 8785).

    Returns the position (path) and text of every number token, so that a caller can
    require integer tokens in integer positions.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise JCSError("invalid_utf8", str(exc)) from exc
    if text.startswith("\ufeff"):
        raise JCSError("bom", "a byte order mark is not allowed")
    scanner = _Scanner(text)
    scanner.document()
    return scanner.numbers


# --------------------------------------------------------------------------- canonicalization


def _utf16_key(key: str) -> bytes:
    return key.encode("utf-16-be", "surrogatepass")


def _string(value: str) -> str:
    out = ['"']
    for ch in value:
        o = ord(ch)
        if 0xD800 <= o <= 0xDFFF:
            raise JCSError("lone_surrogate", "lone surrogate in a string")
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\b":
            out.append("\\b")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\f":
            out.append("\\f")
        elif ch == "\r":
            out.append("\\r")
        elif o < 0x20:
            out.append(f"\\u{o:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _number(value: float) -> str:
    """ECMAScript Number::toString for a finite float."""
    if math.isnan(value) or math.isinf(value):
        raise JCSError("non_finite", "NaN and Infinity cannot be canonicalized")
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    _, digit_tuple, exp = Decimal(repr(abs(value))).as_tuple()
    if not isinstance(exp, int):
        raise JCSError("non_finite", "unexpected special float")
    digits = "".join(map(str, digit_tuple)).rstrip("0") or "0"
    # value = 0.d1d2...dk x 10^n
    n = len(digit_tuple) + exp
    k = len(digits)
    if k <= n <= 21:
        body = digits + "0" * (n - k)
    elif 0 < n <= 21:
        body = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        body = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        esign = "+" if e >= 0 else "-"
        mant = digits[0] if k == 1 else digits[0] + "." + digits[1:]
        body = f"{mant}e{esign}{abs(e)}"
    return sign + body


def _serialize(value: Any) -> str:
    # bool is a subclass of int: test it first, or True would serialize as 1.
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INT:
            raise JCSError("number_range", f"integer {value} is outside +-(2^53-1)")
        return str(value)
    if isinstance(value, float):
        return _number(value)
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_serialize(v) for v in value) + "]"
    if isinstance(value, dict):
        for key in value:
            if not isinstance(key, str):
                raise JCSError("syntax", "object keys must be strings")
        items = sorted(value.items(), key=lambda kv: _utf16_key(kv[0]))
        return "{" + ",".join(_string(k) + ":" + _serialize(v) for k, v in items) + "}"
    raise JCSError("syntax", f"cannot canonicalize {type(value).__name__}")


def canonicalize(value: Any) -> bytes:
    """JCS (RFC 8785) UTF-8 bytes of a parsed JSON value."""
    return _serialize(value).encode("utf-8")


def _no_dupes(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, val in pairs:
        if key in out:
            raise JCSError("duplicate_key", f"duplicate key {key!r}")
        out[key] = val
    return out


def _no_constant(name: str) -> Any:
    raise JCSError("non_finite", f"{name} is not allowed")


def canonicalize_text(text: str) -> bytes:
    """JCS bytes of JSON text. Duplicate keys and NaN/Infinity are rejected."""
    try:
        value = json.loads(text, object_pairs_hook=_no_dupes, parse_constant=_no_constant)
    except json.JSONDecodeError as exc:
        raise JCSError("syntax", str(exc)) from exc
    return canonicalize(value)


def digest(value: Any) -> str:
    """Return sha256:<lowercase hex of SHA-256(JCS(value)))>."""
    return "sha256:" + hashlib.sha256(canonicalize(value)).hexdigest()
