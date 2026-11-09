"""Bridge v1alpha2 contract (docs/contracts/bridge/v1alpha2/): tests T1-T14.

jsonschema is a required dev dependency; a missing import fails the suite.
"""

import copy
import hashlib
import json
import re
from pathlib import Path

import pytest

from tools import bridge_contract as bc
from tools import bridge_fixtures as bf
from tools import jcs

REPO = Path(__file__).resolve().parent.parent
DIR = REPO / "docs" / "contracts" / "bridge" / "v1alpha2"
FIX = DIR / "fixtures"
VEC = DIR / "vectors"
V1 = REPO / "docs" / "spikes" / "1043"

# T1: sha256 of the v1alpha1 contract at the merge base. mctl-api's
# internal/bridge/schema/PROVENANCE pins the first two.
V1ALPHA1_SHA256 = {
    "command.schema.json": "b857f62b817887f56e5fea785c36f57ede44a5647da7c5cfd7e9d5069656dae1",
    "result.schema.json": "c91bb86e973aec1c43b8af1b0743c90d23d389cfdd81e9e5b8ab6042a729354f",
    "fixtures/command.bad-ref-pattern.json": "513988c53e225a412647b17f5472af5a9bb3a9305daeda23caaf70ba4acba01b",
    "fixtures/command.extra-field.json": "718e0fe9eb9f59d83027eac7d13c344b7a64519c21ac750b2133f3ff11ddb3ce",
    "fixtures/command.mutation-missing-approval.json": (
        "b16ab6e16edc5971e6d4da763a910e0a54e8a7af0ad4d134d9e35a175aef011a"
    ),
    "fixtures/command.string-too-long.json": "9a5ab89eb143fe5e8798aaf0e6fa420f17ef12c68989b5d97d3b67a8ebc147a4",
    "fixtures/command.ttl-too-long.json": "620e5c4096c961cd9857391620e5766badc4ba5192259e17f92bc8a3909958ab",
    "fixtures/command.valid.json": "963d3c01ef23282ad8ad86cad57d0339a8be6fff094743cdcdbc2eb99ab992dc",
    "fixtures/result.extra-field.json": "28be9f93d17e33a7396ffdd8c8cb1836f441207d5b11764f6bc98fbd20747e69",
    "fixtures/result.valid.json": "bf4833837d75e9bd9b3367eaed48d661655e6021b6d93525140417aeda870c95",
}

# (fixture, checker, expected reason code or None when valid)
FIXTURES = [
    ("read-command.history.valid.json", "read-command", None),
    ("read-command.preview.valid.json", "read-command", None),
    ("read-command.basket.valid.json", "read-command", None),
    ("read-command.with-order-ref.json", "read-command", "schema_invalid"),
    ("read-command.with-execution-id.json", "read-command", "schema_invalid"),
    ("read-command.with-workflow-id.json", "read-command", "schema_invalid"),
    ("read-command.kind-prepare.json", "read-command", "schema_invalid"),
    ("read-command.params-wrong-kind.json", "read-command", "schema_invalid"),
    ("read-result.history.valid.json", "read-result", None),
    ("read-result.preview.valid.json", "read-result", None),
    ("read-result.basket.valid.json", "read-result", None),
    ("read-result.failed.valid.json", "read-result", None),
    ("read-result.history.max.valid.json", "read-result", None),
    ("read-result.preview.max.valid.json", "read-result", None),
    ("read-result.basket.max.valid.json", "read-result", None),
    ("read-result.at-cap.json", "read-result", None),
    ("read-result.over-cap.json", "read-result", "payload_too_large"),
    ("read-result.digest-mismatch.json", "read-result", "digest_mismatch"),
    ("read-result.failed-with-payload.json", "read-result", "schema_invalid"),
    ("read-result.history.missing-field.json", "read-result", "schema_invalid"),
    ("read-result.preview.missing-field.json", "read-result", "schema_invalid"),
    ("read-result.basket.missing-field.json", "read-result", "schema_invalid"),
    ("read-result.with-binding-echo.json", "read-result", "schema_invalid"),
    ("read-result.extra-field.json", "read-result", "schema_invalid"),
    ("read-result.quantity-float.json", "read-result", "non_integer_token"),
    ("read-result.bad-date-time.json", "read-result", "schema_invalid"),
    ("read-result.basket.empty.valid.json", "read-result", None),
    ("read-result.preview.empty.valid.json", "read-result", None),
    ("read-result.preview.all-unavailable.valid.json", "read-result", None),
    ("read-result.empty-with-basket.json", "read-result", "schema_invalid"),
    ("read-result.nonempty-without-basket.json", "read-result", "schema_invalid"),
    ("approval-basket.valid.json", "approval-basket", None),
    ("approval-basket.unsorted.json", "approval-basket", "unsorted_lines"),
    ("approval-basket.duplicate-item.json", "approval-basket", "duplicate_item"),
    ("approval-basket.total-mismatch.json", "approval-basket", "total_mismatch"),
    ("approval-basket.display-field.json", "approval-basket", "schema_invalid"),
    ("claim-response.v1alpha1-only.valid.json", "claim-response", None),
    ("claim-response.v1alpha2.valid.json", "claim-response", None),
    ("claim-response.extra-field.json", "claim-response", "schema_invalid"),
    ("claim-response.too-many.json", "claim-response", "schema_invalid"),
    ("command-place-order-fake.valid.json", "write-command", None),
    ("command-place-order-fake.v1alpha1-payload.json", "write-command", "schema_invalid"),
    ("command-place-order-fake.wrapped-payload.json", "write-command", "schema_invalid"),
    ("command-place-order-fake.missing-field.json", "write-command", "schema_invalid"),
    ("command-place-order-fake.extra-field.json", "write-command", "schema_invalid"),
    ("command-place-order-fake.install-revision-ref.json", "write-command", "schema_invalid"),
]
CHECKERS = {
    "read-command": bc.check_read_command,
    "read-result": bc.check_read_result,
    "approval-basket": bc.check_approval_basket,
    "write-command": bc.check_write_command,
    "claim-response": bc.check_claim_response,
}


def _raw(name: str) -> bytes:
    return (FIX / name).read_bytes()


def _load(name: str):
    return json.loads(_raw(name))


def _vectors(name: str):
    return json.loads((VEC / name).read_text(encoding="utf-8"))


def _text(doc) -> bytes:
    return json.dumps(doc).encode()


def _reject(check, raw: bytes) -> str:
    with pytest.raises(bc.ContractError) as exc:
        check(raw)
    return exc.value.code


def _with_digest(doc):
    doc = copy.deepcopy(doc)
    doc["payload_digest"] = jcs.digest(doc["payload"])
    return doc


# ---------------------------------------------------------------- T1


@pytest.mark.parametrize("rel", sorted(V1ALPHA1_SHA256))
def test_t1_v1alpha1_unchanged(rel):
    assert hashlib.sha256((V1 / rel).read_bytes()).hexdigest() == V1ALPHA1_SHA256[rel]


def test_t1_every_v1alpha1_fixture_is_pinned():
    on_disk = {p.relative_to(V1).as_posix() for p in V1.rglob("*.json")}
    assert on_disk == set(V1ALPHA1_SHA256)


def test_t1_v1alpha1_fixtures_behave_as_in_spike_tests():
    from tests.test_spike_1043_contracts import CASES

    for fixture, schema, valid, _reason in CASES:
        schema_doc = json.loads((V1 / f"{schema}.schema.json").read_text())
        errors = list(bc._Validator(schema_doc).iter_errors(json.loads((V1 / "fixtures" / fixture).read_text())))
        assert bool(errors) is (not valid), fixture


# ---------------------------------------------------------------- T2


@pytest.mark.parametrize(("fixture", "checker", "reason"), FIXTURES)
def test_t2_fixture_table(fixture, checker, reason):
    check = CHECKERS[checker]
    if reason is None:
        check(_raw(fixture))
    else:
        assert _reject(check, _raw(fixture)) == reason


def test_t2_every_fixture_is_in_the_table():
    listed = {f for f, _, _ in FIXTURES}
    assert listed == {p.name for p in FIX.glob("*.json")}


def test_t2_cap_fixtures_are_exact():
    assert len(_raw("read-result.at-cap.json")) == 16384
    assert len(_raw("read-result.over-cap.json")) == 16385


def test_t2_generated_files_are_current(tmp_path):
    for rel, text in bf.build().items():
        assert (DIR / rel).read_text(encoding="utf-8") == text, rel


def test_measured_sizes_match_readme():
    readme = (DIR / "README.md").read_text(encoding="utf-8")
    for _, size in bf.sizes():
        assert f"{size:,}" in readme


# ---------------------------------------------------------------- T3


@pytest.mark.parametrize(
    "field", ["execution_id", "work_item_id", "order_ref", "workflow_id", "approval_ref", "intent_hash"]
)
@pytest.mark.parametrize("fixture", ["history", "preview", "basket"])
def test_t3_forbidden_fields_rejected(fixture, field):
    doc = _load(f"read-command.{fixture}.valid.json")
    doc[field] = "x"
    assert _reject(bc.check_read_command, _text(doc)) == "schema_invalid"
    # The explicit not/anyOf rejects it even without additionalProperties.
    schema = bc.load_schema("read-command")
    del schema["additionalProperties"]
    assert list(bc._Validator(schema, registry=bc.registry()).iter_errors(doc))


# ---------------------------------------------------------------- T4


def test_t4_versions_do_not_cross_validate():
    v1_command = json.loads((V1 / "fixtures" / "command.valid.json").read_text())
    v1_result = json.loads((V1 / "fixtures" / "result.valid.json").read_text())
    for kind, command in (
        ("read-command", _load("read-command.basket.valid.json")),
        ("write-command", _load("command-place-order-fake.valid.json")),
    ):
        v1_schema = json.loads((V1 / "command.schema.json").read_text())
        assert list(bc._Validator(v1_schema).iter_errors(command)), kind
    assert _reject(bc.check_read_command, _text(v1_command)) == "schema_invalid"
    assert _reject(bc.check_write_command, _text(v1_command)) == "schema_invalid"
    assert _reject(bc.check_read_result, _text(v1_result)) == "schema_invalid"
    v1_result_schema = json.loads((V1 / "result.schema.json").read_text())
    assert list(bc._Validator(v1_result_schema).iter_errors(_load("read-result.failed.valid.json")))


def test_t4_v1alpha2_command_in_v1alpha1_envelope_rejected():
    doc = {"schema_version": "bridge.mctl.ai/v1alpha1", "commands": [_load("read-command.basket.valid.json")]}
    assert _reject(bc.check_claim_response, _text(doc)) == "schema_invalid"
    doc["commands"] = [_load("command-place-order-fake.valid.json")]
    assert _reject(bc.check_claim_response, _text(doc)) == "schema_invalid"


# ---------------------------------------------------------------- T5


def test_t5_jcs_vectors_reproduce():
    data = _vectors("jcs-digest.json")["vectors"]
    assert len(data) >= 22
    for vec in data:
        canonical = jcs.canonicalize_text(vec["input_text"])
        assert canonical == vec["canonical"].encode("utf-8"), vec["name"]
        assert "sha256:" + hashlib.sha256(canonical).hexdigest() == vec["digest"], vec["name"]


def test_t5_categories_covered():
    cats = {v["category"] for v in _vectors("jcs-digest.json")["vectors"]}
    assert {
        "utf16-key-order",
        "control-escapes",
        "del",
        "es-numbers",
        "negative-zero",
        "nested",
        "empty",
        "literals",
        "rfc8785-3.2.2",
        "rfc8785-3.2.3",
    } <= cats
    names = {v["name"] for v in _vectors("jcs-digest.json")["vectors"]}
    assert {"number-1e21", "number-1e20", "number-1e-7"} <= names


def test_t5_bool_is_not_int():
    assert jcs.canonicalize([True, False, 1, 0]) == b"[true,false,1,0]"


def test_jcs_rejects_out_of_profile_values():
    for value in (float("nan"), float("inf"), 2**53, "\ud800"):
        with pytest.raises(jcs.JCSError):
            jcs.canonicalize(value)
    with pytest.raises(jcs.JCSError):
        jcs.canonicalize_text('{"a":1,"a":2}')


def test_jcs_is_stdlib_only():
    import ast

    tree = ast.parse((REPO / "tools" / "jcs.py").read_text())
    import sys

    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            mods.add((node.module or "").split(".")[0])
    assert mods <= set(sys.stdlib_module_names), mods


# ---------------------------------------------------------------- T6


def test_t6_must_reject_entries():
    entries = _vectors("must-reject.json")["entries"]
    assert len(entries) >= 20
    for entry in entries:
        raw = entry["raw"].encode("utf-8")
        for name in entry["applies"]:
            assert _reject(CHECKERS[name], raw) == entry["reason"], entry["name"]


def test_t6_required_entries_present():
    names = {e["name"] for e in _vectors("must-reject.json")["entries"]}
    assert {
        "duplicate-key-top-level",
        "duplicate-key-nested",
        "lone-high-surrogate",
        "lone-low-surrogate",
        "nan",
        "infinity",
        "exponent-overflow-1e400",
        "integer-2-pow-53-plus-1",
        "integer-minus-2-pow-53-minus-1",
        "quantity-1.0",
        "quantity-1e2",
        "quantity-true",
        "escaped-duplicate-key-top-level",
        "escaped-duplicate-quantity-in-basket-line",
    } <= names


def test_t6_invalid_utf8_rejected():
    assert _reject(bc.check_read_result, b'{"a":"\xff"}') == "invalid_utf8"


# ---------------------------------------------------------------- T7


def test_t7_basket_fingerprint_vectors():
    data = _vectors("basket-fingerprint.json")
    by_name = {}
    for vec in data["vectors"]:
        fingerprint = bc.check_approval_basket(vec["input_text"].encode())
        assert fingerprint == vec["fingerprint"], vec["name"]
        assert jcs.canonicalize_text(vec["input_text"]).decode() == vec["canonical"]
        assert fingerprint == "sha256:" + hashlib.sha256(vec["canonical"].encode()).hexdigest()
        by_name[vec["name"]] = vec
    for vec in data["vectors"]:
        if "same_fingerprint_as" in vec:
            assert vec["fingerprint"] == by_name[vec["same_fingerprint_as"]]["fingerprint"]
            assert vec["input_text"] != by_name[vec["same_fingerprint_as"]]["input_text"]
        if "different_from" in vec:
            assert vec["fingerprint"] != by_name[vec["different_from"]]["fingerprint"]
    assert len(json.loads(by_name["thirty-lines"]["input_text"])["lines"]) == 30
    for case in data["must_reject"]:
        assert _reject(bc.check_approval_basket, case["input_text"].encode()) == case["reason"], case["name"]
    assert {c["name"] for c in data["must_reject"]} >= {
        "unsorted-lines",
        "duplicate-item-code",
        "wrong-subtotal",
        "wrong-total",
        "extra-item-name",
        "quantity-1.0",
        "lowercase-item-code",
        "non-ascii-item-code",
    }


def test_t7_basket_limits_in_schema():
    schema = bc.load_schema("approval-basket")
    assert schema["properties"]["subtotal_minor"]["maximum"] == 3000000000
    assert schema["properties"]["total_minor"]["maximum"] == 3020000000
    assert schema["$defs"]["item_code"]["pattern"] == "^FAKE-[A-Z0-9-]{1,32}$"
    assert max(bf.max_basket()["total_minor"], bf.max_basket()["subtotal_minor"]) <= 3020000000


def test_t7_embedded_basket_is_the_fingerprinted_basket():
    doc = _load("read-result.basket.valid.json")
    fingerprint = bc.check_approval_basket(json.dumps(doc["payload"]["approval_basket"]).encode())
    assert fingerprint == jcs.digest(doc["payload"]["approval_basket"])
    assert fingerprint != doc["payload_digest"]  # the transport digest is never the approval fingerprint


# ---------------------------------------------------------------- T8


def _walk_required(schema, defs_root, path=""):
    """Yield (path, schema) for every object schema reachable in properties/items/$ref."""
    stack = [(path, schema)]
    while stack:
        where, node = stack.pop()
        if not isinstance(node, dict):
            continue
        if "$ref" in node and node["$ref"].startswith("#/$defs/"):
            stack.append((where, defs_root[node["$ref"].split("/")[-1]]))
            continue
        if node.get("type") == "object" and "properties" in node:
            yield where, node
            for key, sub in node["properties"].items():
                stack.append((f"{where}/{key}", sub))
        if "items" in node:
            stack.append((where + "/[]", node["items"]))


@pytest.mark.parametrize("name", ["read-command", "read-result", "approval-basket", "write-command", "claim-response"])
def test_t8_schemas_check_and_every_object_is_closed(name):
    schema = bc.load_schema(name)
    bc._Validator.check_schema(schema)
    defs = schema.get("$defs", {})
    roots = [("", schema)] + [(f"$defs/{k}", v) for k, v in defs.items()]
    seen = 0
    for where, root in roots:
        for path, node in _walk_required(root, defs, where):
            seen += 1
            assert node.get("additionalProperties") is False, (name, path)
            assert "required" in node, (name, path)
    assert seen


def test_t8_empty_payload_and_missing_fields_fail():
    base = _load("read-result.preview.valid.json")
    base["payload"] = {}
    assert _reject(bc.check_read_result, _text(_with_digest(base))) == "schema_invalid"
    for fixture in ("history", "preview", "basket"):
        doc = _load(f"read-result.{fixture}.valid.json")

        def drops(node, path=()):
            if isinstance(node, dict):
                for key in list(node):
                    yield (*path, key)
                    yield from drops(node[key], (*path, key))
            elif isinstance(node, list) and node:
                yield from drops(node[0], (*path, 0))

        count = 0
        for path in drops(doc["payload"]):
            variant = copy.deepcopy(doc)
            cursor = variant["payload"]
            for step in path[:-1]:
                cursor = cursor[step]
            if path[-1] in ("next_cursor", "store_name", "approval_basket") and path[:-1] in ((),):
                continue  # optional, or conditional on empty (covered by T11)
            del cursor[path[-1]]
            variant = _with_digest(variant)
            assert _reject(bc.check_read_result, _text(variant)) == "schema_invalid", (fixture, path)
            count += 1
        assert count > 5


def test_t8_basket_fields_each_required():
    basket = _load("approval-basket.valid.json")
    for key in list(basket):
        variant = {k: v for k, v in basket.items() if k != key}
        assert _reject(bc.check_approval_basket, _text(variant)) == "schema_invalid", key
    for key in list(basket["lines"][0]):
        variant = copy.deepcopy(basket)
        del variant["lines"][0][key]
        assert _reject(bc.check_approval_basket, _text(variant)) == "schema_invalid", key


# ---------------------------------------------------------------- T9


def test_t9_sha256sums_match():
    assert (DIR / "SHA256SUMS").read_text(encoding="utf-8") == bc.sums_text()


def test_t9_sums_list_is_complete():
    listed = {line.split("  ", 1)[1] for line in (DIR / "SHA256SUMS").read_text().splitlines()}
    on_disk = {p.relative_to(DIR).as_posix() for p in DIR.rglob("*") if p.is_file() and p.name != "SHA256SUMS"}
    assert listed == on_disk
    assert {"README.md", "vectors/pattern-parity.json", "vectors/must-reject.json"} <= listed


def test_t9_result_digests_equal_jcs_digest():
    count = 0
    for path in FIX.glob("read-result.*.valid.json"):
        doc = json.loads(path.read_bytes())
        if "payload" in doc:
            assert doc["payload_digest"] == jcs.digest(doc["payload"]), path.name
            count += 1
    assert count >= 8


# ---------------------------------------------------------------- T10


def test_t10_synthetic_data_only():
    jwt = re.compile(r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*")
    email = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
    phone = re.compile(r"\+[0-9][0-9 ()-]{7,}[0-9]|\(?[0-9]{3}\)?[ .-][0-9]{3}[ .-][0-9]{4}")
    for path in list(FIX.glob("*.json")) + list(VEC.glob("*.json")):
        text = path.read_text(encoding="utf-8")
        assert "Bearer " not in text and "MCTL_TOKEN=" not in text, path.name
        assert not jwt.search(text), path.name
        assert not email.search(text), path.name
        assert not phone.search(text), path.name


# ---------------------------------------------------------------- T11


def test_t11_empty_reads():
    for name in (
        "read-result.basket.empty.valid.json",
        "read-result.preview.empty.valid.json",
        "read-result.preview.all-unavailable.valid.json",
    ):
        doc = bc.check_read_result(_raw(name))
        assert doc["payload"]["empty"] is True and doc["payload"]["display"] == []
        assert "approval_basket" not in doc["payload"]
    assert _reject(bc.check_read_result, _raw("read-result.empty-with-basket.json")) == "schema_invalid"
    assert _reject(bc.check_read_result, _raw("read-result.nonempty-without-basket.json")) == "schema_invalid"
    basket = _load("approval-basket.valid.json")
    basket.update(lines=[], subtotal_minor=0, total_minor=basket["delivery_fee_minor"] + basket["service_fee_minor"])
    assert _reject(bc.check_approval_basket, _text(basket)) == "schema_invalid"


def test_t11_display_and_unavailable_rules():
    doc = _load("read-result.preview.valid.json")
    bad = copy.deepcopy(doc)
    bad["payload"]["display"][0]["item_code"] = "FAKE-NOPE"
    assert _reject(bc.check_read_result, _text(_with_digest(bad))) == "display_not_in_basket"
    bad = copy.deepcopy(doc)
    bad["payload"]["unavailable_items"] = [bad["payload"]["approval_basket"]["lines"][0]["item_code"]]
    assert _reject(bc.check_read_result, _text(_with_digest(bad))) == "unavailable_overlaps_lines"


def test_t11_payload_cap_is_enforced():
    doc = _load("read-result.basket.valid.json")
    # Bypass the schema bounds to reach the 12288-byte payload cap.
    doc["payload"]["store_name"] = "A" * 13000
    doc = _with_digest(doc)
    schema = bc.load_schema("read-result")
    assert list(bc._Validator(schema, registry=bc.registry()).iter_errors(doc))  # the schema alone rejects it
    assert len(jcs.canonicalize(doc["payload"])) > bc.MAX_PAYLOAD_BYTES


# ---------------------------------------------------------------- T12


def test_t12_claim_envelope():
    only = _load("claim-response.v1alpha1-only.valid.json")
    assert list(only) == ["schema_version", "commands"]
    assert only["schema_version"] == "bridge.mctl.ai/v1alpha1"
    assert only["commands"] == [json.loads((V1 / "fixtures" / "command.valid.json").read_text())]
    full = _load("claim-response.v1alpha2.valid.json")
    kinds = [c["schema_version"] for c in full["commands"]]
    assert kinds == ["bridge.mctl.ai/v1alpha1", bf.V2, bf.V2]
    assert [c["kind"] for c in full["commands"]] == ["place_order_fake", "read_history", "place_order_fake"]
    assert len(_load("claim-response.too-many.json")["commands"]) == 11
    full["commands"] = full["commands"] * 4
    assert _reject(bc.check_claim_response, _text(full)) == "schema_invalid"


def test_t12_claim_envelope_enforces_basket_rules_on_v2_writes():
    full = _load("claim-response.v1alpha2.valid.json")
    bc.check_claim_response(_text(full))
    write = next(c for c in full["commands"] if c["schema_version"] == bf.V2 and c["kind"] == "place_order_fake")
    write["payload"]["total_minor"] += 1
    assert _reject(bc.check_claim_response, _text(full)) == "total_mismatch"


# ---------------------------------------------------------------- T13


def test_t13_date_time_pattern_enforced_in_python():
    import jsonschema

    doc = _load("read-result.bad-date-time.json")
    schema = bc.load_schema("read-result")
    # Python does not enforce `format`: without the pattern the document would validate.
    schema["properties"]["completed_at"].pop("pattern")
    assert not list(jsonschema.Draft202012Validator(schema, registry=bc.registry()).iter_errors(doc))
    with pytest.raises(bc.ContractError, match="completed_at"):
        bc.check_read_result(_raw("read-result.bad-date-time.json"))


# ---------------------------------------------------------------- T14


def test_t14_write_command_property_lists():
    v1 = json.loads((V1 / "command.schema.json").read_text())
    v2 = bc.load_schema("write-command")
    assert set(v2["properties"]) == set(v1["properties"])
    assert "install_revision_ref" not in v2["properties"]
    assert set(v2["required"]) == set(v1["required"]) | {"approval_ref"}
    assert v2["additionalProperties"] is False
    for name, spec in v1["properties"].items():
        if name not in {"schema_version", "kind", "issued_at", "expires_at", "payload"}:
            assert v2["properties"][name] == spec, name
    assert v2["properties"]["ttl_seconds"]["maximum"] == 900
    assert v2["properties"]["payload"] == {"$ref": "approval-basket.schema.json"}


def test_t14_property_list_test_is_proven_red():
    v1 = json.loads((V1 / "command.schema.json").read_text())
    v2 = bc.load_schema("write-command")
    widened = {**v2["properties"], "extra_name": {"type": "string"}}
    assert set(widened) != set(v1["properties"])
    narrowed = [r for r in v2["required"] if r != "approval_ref"]
    assert set(narrowed) != set(v1["required"]) | {"approval_ref"}


def test_t14_valid_fixture_and_payload_bytes():
    doc = bc.check_write_command(_raw("command-place-order-fake.valid.json"))
    canonical = jcs.canonicalize(doc["payload"]).decode()
    minimal = next(v for v in _vectors("basket-fingerprint.json")["vectors"] if v["name"] == "minimal")
    assert canonical == minimal["canonical"]


def test_t14_write_result_is_v1alpha1():
    v1_result = json.loads((V1 / "fixtures" / "result.valid.json").read_text())
    v1_schema = json.loads((V1 / "result.schema.json").read_text())
    assert not list(bc._Validator(v1_schema).iter_errors(v1_result))
    assert _reject(bc.check_read_result, _text(v1_result)) == "schema_invalid"


def test_t14_check_write_command_rejects_every_v1alpha1_command():
    for path in sorted((V1 / "fixtures").glob("command.*.json")):
        assert _reject(bc.check_write_command, path.read_bytes()) in {"schema_invalid"}, path.name


# ---------------------------------------------------------------- patterns


def _walk_patterns(node, path=""):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "pattern" and isinstance(value, str):
                yield path, value
            else:
                yield from _walk_patterns(value, f"{path}/{key}")
    elif isinstance(node, list):
        for n, value in enumerate(node):
            yield from _walk_patterns(value, f"{path}/{n}")


FORBIDDEN_PATTERN = re.compile(r"\\[dDwWsSbBZzAG1-9]|\(\?|\\[pPkx]")


def _pattern_ok(pattern: str) -> bool:
    return not FORBIDDEN_PATTERN.search(pattern) and pattern.startswith("^") and pattern.endswith("$")


def test_pattern_lint_over_every_schema():
    total = 0
    for name in bc.SCHEMA_FILES:
        for path, pattern in _walk_patterns(bc.load_schema(name)):
            total += 1
            assert _pattern_ok(pattern), (name, path, pattern)
            assert "$" not in pattern[:-1] and "^" not in pattern[1:], (name, path, pattern)
    assert total > 40


def test_pattern_lint_turns_red_on_backslash_d():
    assert not _pattern_ok(r"^\d{4}-[0-9]{2}$")
    assert not _pattern_ok(r"^(?=a)a$")
    assert not _pattern_ok(r"^a\sb$")
    assert _pattern_ok("^[0-9]{4}$")


def test_pattern_parity_vectors():
    data = _vectors("pattern-parity.json")
    assert {c["id"] for c in data["cases"]} >= {"placed_on", "issued_at", "item_code", "item_name", "store_ref"}
    for case in data["cases"]:
        schema = json.loads((DIR / case["schema"]).read_text(encoding="utf-8"))
        pattern = schema
        for part in case["pointer"].lstrip("/").split("/"):
            pattern = pattern[part]
        assert isinstance(pattern, str)
        # Same verdict with and without re.ASCII: the pattern itself carries no Unicode semantics.
        for flags in (re.ASCII, 0):
            for text in case["accept"]:
                assert re.fullmatch(pattern, text, flags), (case["id"], text, flags)
            for text in case["reject"]:
                assert not re.fullmatch(pattern, text, flags), (case["id"], text, flags)


def test_pattern_parity_goes_red_when_digits_revert_to_backslash_d():
    case = next(c for c in _vectors("pattern-parity.json")["cases"] if c["id"] == "placed_on")
    reverted = r"^\d{4}-\d{2}-\d{2}$"
    assert not _pattern_ok(reverted)
    assert any(re.fullmatch(reverted, text) for text in case["reject"])
