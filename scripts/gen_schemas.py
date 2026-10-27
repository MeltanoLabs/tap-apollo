"""Generate tap-apollo stream JSON schemas from Apollo's OpenAPI spec.

Usage: python -I gen_schemas.py <spec.json> <out_dir> [<samples_dir>]

<samples_dir> optionally holds real API records, one `<stream>.json` array per stream.
Apollo's spec and examples omit many fields, so merging real records fills the gaps.
Don't commit the samples: they contain customer data.

Fields already in a schema in <out_dir> are only ever widened (new nested fields, integer to
number, or a string where the data disagrees), so regenerating never narrows a column that
targets have already created.
"""

import json
import re
import sys
from datetime import datetime

spec = json.load(open(sys.argv[1]))
out_dir = sys.argv[2]
samples_dir = sys.argv[3] if len(sys.argv) > 3 else None

# stream name -> (method, path, records key or None for top-level array)
STREAMS = {
    "contacts": ("post", "/contacts/search", "contacts"),
    "accounts": ("post", "/accounts/search", "accounts"),
    "opportunities": ("get", "/opportunities/search", "opportunities"),
    "sequences": ("post", "/emailer_campaigns/search", "emailer_campaigns"),
    "tasks": ("post", "/tasks/search", "tasks"),
    "users": ("get", "/users/search", "users"),
    "email_accounts": ("get", "/email_accounts", "email_accounts"),
    "lists": ("get", "/labels", None),
    "custom_fields": ("get", "/typed_custom_fields", "typed_custom_fields"),
    "contact_stages": ("get", "/contact_stages", "contact_stages"),
    "account_stages": ("get", "/account_stages", "account_stages"),
    "opportunity_stages": ("get", "/opportunity_stages", "opportunity_stages"),
    "phone_calls": ("get", "/phone_calls/search", "phone_calls"),
    "emailer_messages": ("get", "/emailer_messages/search", "emailer_messages"),
}

ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


def resolve(node, depth=0):
    if isinstance(node, dict) and "$ref" in node and depth < 20:
        target = spec
        for part in node["$ref"].lstrip("#/").split("/"):
            target = target[part]
        return resolve(target, depth + 1)
    return node


def from_openapi(node, depth=0):
    """Convert an OpenAPI schema node into a nullable JSON schema."""
    node = resolve(node)
    if depth > 12 or not isinstance(node, dict):
        return {}
    for combo in ("allOf", "oneOf", "anyOf"):
        if combo in node:
            merged = {}
            for sub in node[combo]:
                merged = merge(merged, from_openapi(sub, depth + 1))
            return merged
    typ = node.get("type")
    if typ == "object" or "properties" in node:
        props = {k: from_openapi(v, depth + 1) for k, v in node.get("properties", {}).items()}
        return {"type": "object", "properties": props}
    if typ == "array":
        return {"type": "array", "items": from_openapi(node.get("items", {}), depth + 1)}
    if typ in ("string", "integer", "number", "boolean"):
        out = {"type": typ}
        if typ == "string" and node.get("format") == "date-time":
            out["format"] = "date-time"
        return out
    return {}


def infer(value):
    """Infer a JSON schema from an example value."""
    if value is None:
        return {}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, str):
        if ISO_RE.match(value):
            return {"type": "string", "format": "date-time"}
        return {"type": "string"}
    if isinstance(value, list):
        items = {}
        for item in value:
            items = merge(items, infer(item))
        return {"type": "array", "items": items}
    if isinstance(value, dict):
        return {"type": "object", "properties": {k: infer(v) for k, v in value.items()}}
    return {}


def merge(a, b):
    """Merge two inferred schemas, widening types where they disagree."""
    if not a:
        return b
    if not b:
        return a
    ta, tb = a.get("type"), b.get("type")
    if ta == tb == "object":
        props = dict(a.get("properties", {}))
        for k, v in b.get("properties", {}).items():
            props[k] = merge(props.get(k, {}), v)
        return {"type": "object", "properties": props}
    if ta == tb == "array":
        return {"type": "array", "items": merge(a.get("items", {}), b.get("items", {}))}
    if {ta, tb} == {"integer", "number"}:
        return {"type": "number"}
    if ta == tb:
        out = dict(a)
        if a.get("format") != b.get("format"):
            out.pop("format", None)
        return out
    return {"type": "any"}  # conflicting types: accept any value


# Accepts any JSON value; used for fields that were null in every example.
ANY_TYPE = ["string", "number", "integer", "boolean", "object", "array", "null"]
# Objects keyed by team-specific IDs, so their keys can't be listed.
FREE_FORM_OBJECTS = {"typed_custom_fields", "custom_field_errors"}
APOLLO_ID = re.compile(r"^[0-9a-f]{24}$")
STRING_SUFFIXES = ("_at", "_id", "_url", "_cd", "_name", "_reason", "_token", "_date", "_symbol")


def guess_type(key):
    """Guess the type of a field that was null in every example, from its name."""
    if key.endswith(STRING_SUFFIXES) or key in ("city", "state", "country", "bounce"):
        return "string"
    if key.startswith(("is_", "has_")):
        return "boolean"
    return None


def finalize(node, key=""):
    """Make every node nullable; untyped nodes accept any JSON value."""
    typ = node.get("type") or guess_type(key)
    if typ is None or typ == "any":
        return {"type": ANY_TYPE}
    out = {"type": [typ, "null"]}
    if typ == "object":
        props = node.get("properties", {})
        if props:
            out["properties"] = {k: finalize(v, k) for k, v in sorted(props.items())}
    elif typ == "array":
        items = finalize(node.get("items", {}))
        if items:
            out["items"] = items
    elif typ == "string":
        if node.get("format") == "date-time" or key.endswith("_at"):
            out["format"] = "date-time"
    return out


def base_types(node):
    types = node.get("type", [])
    return set(types if isinstance(types, list) else [types]) - {"null"}


def widen(old, new):
    """Combine an existing field definition with a regenerated one without narrowing it.

    The result keeps every type and nested field `old` allows. It only gains nested fields,
    widens integer to number, or accepts any value where the two disagree.
    """
    if not new:
        return old
    old_types, new_types = base_types(old), base_types(new)
    if old_types >= set(ANY_TYPE) - {"null"}:
        return old
    if old_types == new_types:
        out = dict(old)
        if "object" in old_types and "properties" in new:
            props = dict(old.get("properties", {}))
            for field, sub_new in new["properties"].items():
                props[field] = widen(props[field], sub_new) if field in props else sub_new
            out["properties"] = props
        if "array" in old_types and "items" in old and "items" in new:
            out["items"] = widen(old["items"], new["items"])
        return out
    if old_types | new_types == {"integer", "number"}:
        return {"type": ["number", "null"]}
    return {"type": ANY_TYPE}


def drop_id_keys(node, key=""):
    """Drop the listed fields of objects keyed by Apollo IDs, such as custom field values.

    Those keys differ between Apollo teams, so listing them would only fit the team the
    samples came from.
    """
    props = node.get("properties", {})
    if key in FREE_FORM_OBJECTS or any(APOLLO_ID.match(k) for k in props):
        node["properties"] = {}
    for field, child in node.get("properties", {}).items():
        drop_id_keys(child, field)
    if isinstance(node.get("items"), dict):
        drop_id_keys(node["items"])


def open_objects(node):
    """Let every nested object keep keys its schema doesn't list.

    Apollo adds fields without notice, and the SDK drops unlisted keys unless
    `additionalProperties` is set. It also conforms list items that may be objects as if
    they had `properties` and crashes without them, so objects always get `properties`.
    """
    types = node.get("type", [])
    if "object" in (types if isinstance(types, list) else [types]):
        node.setdefault("properties", {})
        node["additionalProperties"] = True
    for child in node.get("properties", {}).values():
        open_objects(child)
    if isinstance(node.get("items"), dict):
        open_objects(node["items"])


def examples(op):
    content = resolve(op["responses"]["200"]).get("content", {}).get("application/json", {})
    values = []
    if "example" in content:
        values.append(content["example"])
    for ex in content.get("examples", {}).values():
        val = resolve(ex).get("value")
        if isinstance(val, str):
            try:
                # Some of Apollo's examples contain trailing commas.
                val = json.loads(re.sub(r",(\s*[\]}])", r"\1", val))
            except json.JSONDecodeError:
                continue
        values.append(val)
    return values


for name, (method, path, key) in STREAMS.items():
    op = spec["paths"][path][method]
    resp = resolve(
        resolve(op["responses"]["200"])
        .get("content", {})
        .get("application/json", {})
        .get("schema", {})
    )
    if key is None:
        item = from_openapi(resp.get("items", {})) if resp.get("type") == "array" else {}
    else:
        arr = resolve(resp.get("properties", {}).get(key, {}))
        item = from_openapi(arr.get("items", {})) if arr else {}
    for ex in examples(op):
        records = ex if key is None else (ex or {}).get(key, [])
        for rec in records or []:
            item = merge(item, infer(rec))
    if samples_dir:
        try:
            samples = json.load(open(f"{samples_dir}/{name}.json"))
        except FileNotFoundError:
            samples = []
        for rec in samples:
            item = merge(item, infer(rec))
    schema = finalize(item)
    if not schema.get("properties"):
        sys.exit(f"no properties found for {name}")
    try:
        previous = json.load(open(f"{out_dir}/{name}.json"))["properties"]
    except FileNotFoundError:
        previous = {}
    for field, old in previous.items():
        schema["properties"][field] = widen(old, schema["properties"].get(field))
    for field, prop in schema["properties"].items():
        if field in FREE_FORM_OBJECTS:
            schema["properties"][field] = {"type": ["object", "null"]}
        elif base_types(prop) >= set(ANY_TYPE) - {"null"}:
            # A column that allows both objects and scalars breaks some targets (target-postgres
            # makes it jsonb, then rejects plain strings), so the tap emits these as strings,
            # JSON-encoding any other value.
            schema["properties"][field] = {"type": ["string", "null"]}
    schema["properties"]["id"] = {"type": "string"}
    for field, prop in schema["properties"].items():
        drop_id_keys(prop, field)
        open_objects(prop)
    schema["type"] = "object"
    schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", **schema}
    with open(f"{out_dir}/{name}.json", "w") as fh:
        json.dump(schema, fh, indent=2)
        fh.write("\n")
    print(f"{name}: {len(schema['properties'])} properties")
