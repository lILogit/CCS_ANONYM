#!/usr/bin/env python3
"""
ccs_anon.py - privacy-preserving calculations over CCS models.

  model.md --ontology--> English terms --anonymize--> IDs only --LLM--> Python over IDs
           --validate--> run locally with real values --> <model>.local.py

Sensitive values never leave this machine. Entity and key names are normalized to a
controlled English ontology (ontology.json: Class.property terms with aliases), so the
same concept always gets the same name. Unknown names are mapped once by the LLM (names
and units only, never values) and cached as aliases; known names resolve locally.

The single output file <model>.local.py holds the ID conversion table (ID_TABLE) and the
LLM calculation with real values; it runs on its own and can be reloaded with IdTable.load().

Usage:
  python ccs_anon.py cc_model.md "Total yearly running cost of the car"
  python ccs_anon.py cc_model.md "..." --dry-run          # show what would be sent, stop
  python ccs_anon.py cc_model.md "..." --code-file x.py   # skip the LLM, use your own code
  python ccs_anon.py cc_model.md "..." --public type      # keep 'type' values in clear text
  python ccs_anon.py cc_model.md "..." --table t.csv      # extra table export (.json/.csv/.md)
  python ccs_anon.py cc_model.md "..." --no-ontology-llm  # map names from ontology.json only
"""
from __future__ import annotations

import argparse
import ast
import builtins
import csv
import json
import keyword
import math
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable

ENTITY_RE = re.compile(r"\$\s*(\w+)\s*\{(.*?)\}", re.DOTALL)
KEY_RE = re.compile(r"(\w+)\s*:")
# Czech/EU numbers: 250.000 / 250 000 (thousands), 12,5 (decimal comma), 1.5
GROUPED = r"[-+]?\d{1,3}(?:[.\s]\d{3})+(?:,\d+)?"
NUM_RE = re.compile(rf"{GROUPED}|[-+]?\d+(?:[.,]\d+)?")
RESERVED = set(dir(builtins)) | set(keyword.kwlist) | {"math", "result", "ID_TABLE"}
ONTOLOGY_PATH = Path(__file__).with_name("ontology.json")
CLASS_RE = re.compile(r"[A-Z][A-Za-z0-9]*")
PROP_RE = re.compile(r"[a-z][a-z0-9_]*")


def parse_number(s: str) -> float:
    s = s.strip()
    if re.fullmatch(GROUPED, s):
        s = re.sub(r"[.\s]", "", s)
    return float(s.replace(",", "."))


def split_value(raw: str) -> tuple[object, str]:
    """'1500 Kč/ monthly' -> (1500.0, 'Kč/monthly'); 'Skoda' -> ('Skoda', '')."""
    m = NUM_RE.search(raw)
    if not m:
        return raw, ""
    return parse_number(m.group()), re.sub(r"\s+", "", raw[m.end():])


def slug(s: str) -> str:
    """ASCII snake_case: 'Dálniční známka' -> 'dalnicni_znamka'."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")
    return s if s and not s[0].isdigit() else f"f_{s}"


def parse_entities(text: str) -> list[dict]:
    """[{name, name_span, items: [{key, key_span, raw, val_span, unit}]}] in source order."""
    entities = []
    for ent in ENTITY_RE.finditer(text):
        body, off = ent.group(2), ent.start(2)
        keys = list(KEY_RE.finditer(body))
        items = []
        for i, k in enumerate(keys):
            end = keys[i + 1].start() if i + 1 < len(keys) else len(body)
            raw = body[k.end():end].strip()
            items.append({"key": k.group(1), "key_span": (off + k.start(1), off + k.end(1)),
                          "raw": raw, "val_span": (off + k.end(), off + end), "unit": split_value(raw)[1]})
        entities.append({"name": ent.group(1), "name_span": ent.span(1), "items": items})
    return entities


# --------------------------------------------------------------------------- #
# Ontology - controlled English vocabulary for entity (class) and key (property) names
# --------------------------------------------------------------------------- #

# mapper(request, existing_ontology) -> [{ref, class, class_label, class_same_as, keys: [...]}]
Mapper = Callable[[list, dict], list]


class Ontology:
    """Classes (PascalCase) and terms 'Class.property' (snake_case), each with aliases.

    An entity resolves locally when one of its candidate classes has a term for every key;
    otherwise it is sent to the mapper (the LLM), whose answer is validated and learned.
    """
    FORMAT = "ccs-ontology/1"

    def __init__(self, path: Path | None = None):
        self.path = path
        data = json.loads(path.read_text(encoding="utf-8")) if path and path.is_file() else {}
        self.classes: dict = data.get("classes", {})
        self.terms: dict = data.get("terms", {})
        self.dirty = False

    def summary(self) -> dict:
        """Names and labels only - what the mapper may see."""
        return {"classes": {c: d["label"] for c, d in self.classes.items()},
                "terms": {t: d["label"] for t, d in self.terms.items()}}

    def _prop(self, cls: str, key: str) -> str | None:
        s = slug(key)
        for tid, d in self.terms.items():
            c, p = tid.split(".", 1)
            if c == cls and (s == p or s in d["aliases"]):
                return p
        return None

    def lookup(self, entity: str, keys: list[str]) -> tuple[str, dict] | None:
        s = slug(entity)
        for c, d in self.classes.items():
            if s == slug(c) or s in d["aliases"]:
                props = {k: self._prop(c, k) for k in keys}
                if all(props.values()):
                    return c, props
        return None

    def learn(self, entity: str, m: dict) -> tuple[str, dict]:
        cls = m["class"]
        d = self.classes.setdefault(cls, {"label": m["class_label"], "same_as": m["class_same_as"], "aliases": []})
        if slug(entity) != slug(cls) and slug(entity) not in d["aliases"]:
            d["aliases"].append(slug(entity))
        props = {}
        for k in m["keys"]:
            t = self.terms.setdefault(f"{cls}.{k['property']}",
                                      {"label": k["label"], "same_as": k["same_as"], "aliases": []})
            if slug(k["key"]) != k["property"] and slug(k["key"]) not in t["aliases"]:
                t["aliases"].append(slug(k["key"]))
            props[k["key"]] = k["property"]
        self.dirty = True
        return cls, props

    @staticmethod
    def _valid(req: dict, m: dict) -> bool:
        return (bool(CLASS_RE.fullmatch(m.get("class", "")))
                and sorted(k.get("key") for k in m.get("keys", [])) == sorted(req["keys"])
                and all(PROP_RE.fullmatch(k.get("property", "")) for k in m["keys"]))

    @staticmethod
    def fallback(entity: str, keys: list[str]) -> tuple[str, dict]:
        cls = "".join(w.capitalize() for w in slug(entity).split("_")) or "Entity"
        return (cls if CLASS_RE.fullmatch(cls) else "Entity"), {k: slug(k) for k in keys}

    def resolve(self, entities: list[dict], mapper: Mapper | None = None) -> list[tuple[str, dict, str]]:
        """Per entity: (class, {key: property}, status) with status known | learned | fallback."""
        out: list = [None] * len(entities)
        todo: dict[tuple, list[int]] = {}
        for i, e in enumerate(entities):
            keys = [it["key"] for it in e["items"]]
            hit = self.lookup(e["name"], keys)
            if hit:
                out[i] = (*hit, "known")
            else:
                todo.setdefault((e["name"], tuple(keys)), []).append(i)

        if todo and mapper:
            request = [{"ref": n, "entity": name, "keys": list(keys),
                        "units": {it["key"]: it["unit"] for it in entities[idx[0]]["items"] if it["unit"]}}
                       for n, ((name, keys), idx) in enumerate(todo.items())]
            answers = {m.get("ref"): m for m in mapper(request, self.summary())}
            for req, idx in zip(request, todo.values()):
                m = answers.get(req["ref"])
                if m and self._valid(req, m):
                    cls, props = self.learn(req["entity"], m)
                    for i in idx:
                        out[i] = (cls, props, "learned")

        for (name, keys), idx in todo.items():
            for i in idx:
                if out[i] is None:
                    out[i] = (*self.fallback(name, list(keys)), "fallback")

        for cls, props, _ in out:                     # unique property per entity
            seen = set()
            for k, p in props.items():
                q, n = p, 2
                while q in seen:
                    q, n = f"{p}_{n}", n + 1
                props[k] = q
                seen.add(q)
        return out

    def save(self) -> None:
        if self.path and self.dirty:
            data = {"format": self.FORMAT,
                    "classes": dict(sorted(self.classes.items())), "terms": dict(sorted(self.terms.items()))}
            self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            self.dirty = False


# --------------------------------------------------------------------------- #
# ID <-> value conversion table
# --------------------------------------------------------------------------- #

@dataclass
class Record:
    id: str           # N.. number, T.. text
    entity: str       # as written in the model
    key: str          # as written in the model
    term: str         # ontology term 'Class.property' (English)
    var: str          # readable Python name used in the de-anonymized script
    value: object     # float or str
    unit: str         # kept in clear - the LLM needs it for semantics
    raw: str          # original text as written in the model


class IdTable:
    COLUMNS = tuple(Record.__dataclass_fields__)

    def __init__(self, records: list[Record], mapping: list[tuple] = ()):
        self.records = records
        self.by_id = {r.id: r for r in records}
        self.mapping = list(mapping)      # (entity, key, term, status) for every key incl. public ones

    # --- build ----------------------------------------------------------- #
    @classmethod
    def from_model(cls, text: str, public_keys: set[str] = frozenset(),
                   ontology: Ontology | None = None, mapper: Mapper | None = None) -> tuple[str, "IdTable"]:
        """Normalize names to the ontology and anonymize values. Returns (text with IDs, table)."""
        entities = parse_entities(text)
        resolved = (ontology or Ontology()).resolve(entities, mapper)
        records, mapping, edits = [], [], []
        count, taken = {"N": 0, "T": 0}, set(RESERVED)
        for e, (klass, props, status) in zip(entities, resolved):
            edits.append((*e["name_span"], klass))
            for it in e["items"]:
                key, prop = it["key"], props[it["key"]]
                mapping.append((e["name"], key, f"{klass}.{prop}", status))
                edits.append((*it["key_span"], prop))
                if key in public_keys:
                    continue
                value, unit = split_value(it["raw"])
                kind = "T" if isinstance(value, str) else "N"
                count[kind] += 1
                rid = f"{kind}{count[kind]:02d}"
                var = prop if prop not in taken else f"{slug(klass)}_{prop}"
                while var in taken:
                    var += "_"
                taken.add(var)
                records.append(Record(rid, e["name"], key, f"{klass}.{prop}", var, value, unit, it["raw"]))
                edits.append((*it["val_span"], f" {rid} [{unit}]  " if unit else f" {rid}  "))
        for start, end, new in sorted(edits, reverse=True):
            text = text[:start] + new + text[end:]
        return text, cls(records, mapping)

    def mapping_text(self) -> str:
        w = max((len(f"{e}.{k}") for e, k, _, _ in self.mapping), default=0)
        return "\n".join(f"  {f'{e}.{k}':<{w}} -> {t}  ({s})" for e, k, t, s in self.mapping)

    # --- lookup ---------------------------------------------------------- #
    def value(self, rid: str):
        return self.by_id[rid].value

    def id_of(self, value) -> str | None:
        return next((r.id for r in self.records if value in (r.value, r.raw)), None)

    def values(self) -> dict:
        return {r.id: r.value for r in self.records}

    def deanonymize_text(self, text: str, use: str = "value") -> str:
        """Replace IDs in any text by their 'value', 'raw' or 'var'."""
        return re.sub(r"\b[NT]\d{2,}\b",
                      lambda m: str(getattr(self.by_id[m.group()], use)) if m.group() in self.by_id else m.group(),
                      text)

    def scrub(self, prompt: str) -> str:
        """Replace original values the user typed into the prompt by their IDs.
        Numbers match by value in any format; text matches whole words, case-insensitive."""
        for r in sorted((r for r in self.records if r.id[0] == "T"), key=lambda r: -len(r.raw)):
            prompt = re.sub(rf"(?<!\w){re.escape(r.raw)}(?!\w)", r.id, prompt, flags=re.IGNORECASE)
        numbers = {r.value: r.id for r in self.records if r.id[0] == "N"}
        return re.sub(rf"(?<![\w.,]){NUM_RE.pattern}(?!\d)",
                      lambda m: numbers.get(parse_number(m.group()), m.group()), prompt)

    # --- rendering / persistence ----------------------------------------- #
    def rows(self) -> list[dict]:
        return [asdict(r) for r in self.records]

    def to_markdown(self) -> str:
        lines = ["| " + " | ".join(self.COLUMNS) + " |", "|" + "---|" * len(self.COLUMNS)]
        lines += ["| " + " | ".join(str(row[c]).replace("|", "\\|") for c in self.COLUMNS) + " |"
                  for row in self.rows()]
        return "\n".join(lines)

    def to_text(self) -> str:
        rows = [self.COLUMNS] + [tuple(str(row[c]) for c in self.COLUMNS) for row in self.rows()]
        w = [max(len(row[i]) for row in rows) for i in range(len(self.COLUMNS))]
        lines = [" | ".join(c.ljust(w[i]) for i, c in enumerate(row)) for row in rows]
        lines.insert(1, "-+-".join("-" * x for x in w))
        return "\n".join("  " + l for l in lines)

    def save(self, path: Path) -> Path:
        ext = path.suffix.lower()
        if ext == ".json":
            path.write_text(json.dumps(self.rows(), ensure_ascii=False, indent=2), encoding="utf-8")
        elif ext == ".csv":
            with path.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=self.COLUMNS)
                w.writeheader()
                w.writerows(self.rows())
        elif ext == ".md":
            path.write_text("# ID conversion table - SENSITIVE, keep local\n\n" + self.to_markdown() + "\n",
                            encoding="utf-8")
        else:
            raise ValueError(f"unsupported table format: {ext} (use .json, .csv or .md)")
        path.chmod(0o600)
        return path

    @classmethod
    def load(cls, path: Path) -> "IdTable":
        """Load from .json, .csv or a generated .local.py script (its ID_TABLE)."""
        ext = path.suffix.lower()
        if ext == ".csv":
            with path.open(encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            for row in rows:                               # CSV is untyped
                if row["id"][0] == "N":
                    row["value"] = float(row["value"])
        elif ext == ".py":
            tree = ast.parse(path.read_text(encoding="utf-8"))
            node = next(n.value for n in tree.body if isinstance(n, ast.Assign)
                        and getattr(n.targets[0], "id", None) == "ID_TABLE")
            rows = [{"id": k, **v} for k, v in ast.literal_eval(node).items()]
        else:
            rows = json.loads(path.read_text(encoding="utf-8"))
        return cls([Record(**{c: row.get(c, "") for c in cls.COLUMNS}) for row in rows])


# --------------------------------------------------------------------------- #
# LLM - receives only the anonymized model + scrubbed prompt
# --------------------------------------------------------------------------- #

SYSTEM = """You write Python calculation code for an anonymized data model.
Every sensitive value is replaced by an ID: N.. = number, T.. = text. Units are shown in [brackets].
The IDs are pre-defined Python variables at run time (N.. are floats, T.. are str). You never see real values.

Rules for your code:
- Output ONLY Python code, no markdown fences, no prose outside comments.
- Use the IDs directly as variables. Never invent or hard-code their values.
- Straight-line code only: assignments, arithmetic, comparisons, if/else, dict/list/tuple literals.
  No imports, loops, functions, lambdas, comprehensions or subscripts.
- Allowed calls: math.<fn>, abs, round, min, max, sum, len, float, int, str. Exponents must be literal numbers.
- Handle unit conversion explicitly (e.g. monthly -> yearly *12) and comment each step.
- Finish by assigning a dict named `result` mapping human-readable labels to computed values."""


ONTOLOGY_SYSTEM = """You normalize field names of data models into a controlled English ontology.
You receive entity names with their keys (and units), plus the existing ontology. You never see values.

For every requested entity (identified by `ref`):
- class: one PascalCase singular English noun for what the entity is (e.g. Vehicle, RealEstate, BankAccount).
  Infer it from the keys when the entity name is generic (e.g. "property", "item").
- for each key, property: descriptive English snake_case, ASCII only. Translate non-English names
  (e.g. Czech) and fix typos. No units or periods in the name (units stay separate).
- REUSE an existing class/term when the meaning matches; create new ones only when nothing fits.
  Never map two keys of one entity to the same property.
- label: short English human-readable name. same_as: the exact schema.org IRI when an equivalent
  exists (e.g. https://schema.org/mileageFromOdometer), otherwise an empty string.
Return every requested key exactly as written."""

_S = {"type": "string"}
ONTOLOGY_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["entities"],
    "properties": {"entities": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["ref", "class", "class_label", "class_same_as", "keys"],
        "properties": {
            "ref": {"type": "integer"}, "class": _S, "class_label": _S, "class_same_as": _S,
            "keys": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["key", "property", "label", "same_as"],
                "properties": {"key": _S, "property": _S, "label": _S, "same_as": _S}}}}}}},
}


def load_env(path: Path = Path(__file__).with_name(".env")) -> None:
    """Minimal .env loader (KEY=value lines). Real environment variables take precedence."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if value:
            os.environ.setdefault(key.strip(), value)


def have_key() -> bool:
    load_env()
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def claude(system: str, user: str, model: str, **extra) -> str:
    import anthropic

    if not have_key():
        sys.exit(f"No API key: set ANTHROPIC_API_KEY in {Path(__file__).with_name('.env')} or the environment")
    resp = anthropic.Anthropic().beta.messages.create(
        model=model,
        max_tokens=16000,
        system=system,
        thinking={"type": "adaptive"},
        betas=["server-side-fallback-2026-07-01"],
        extra_body={"fallbacks": "default"},   # SDK 0.86 has no typed kwarg yet
        messages=[{"role": "user", "content": user}],
        **extra,
    )
    if resp.stop_reason == "refusal":
        sys.exit(f"LLM refused: {resp.stop_details}")
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def llm_map_terms(request: list, existing: dict, model: str) -> list:
    """Ontology mapping - sends entity/key names, units and the ontology's names only."""
    user = json.dumps({"existing_ontology": existing, "entities": request}, ensure_ascii=False, indent=1)
    text = claude(ONTOLOGY_SYSTEM, user, model,
                  output_config={"format": {"type": "json_schema", "schema": ONTOLOGY_SCHEMA}})
    return json.loads(text)["entities"]


def ollama_map_terms(request: list, existing: dict, model: str) -> list:
    """Ontology mapping with a local Ollama model - nothing leaves the machine."""
    import httpx

    host = os.environ.get("OLLAMA_HOST", "localhost:11434")
    url = (host if "://" in host else f"http://{host}") + "/api/chat"
    user = json.dumps({"existing_ontology": existing, "entities": request}, ensure_ascii=False, indent=1)
    try:
        r = httpx.post(url, timeout=600, json={
            "model": model, "stream": False, "format": ONTOLOGY_SCHEMA, "options": {"temperature": 0},
            "messages": [{"role": "system", "content": ONTOLOGY_SYSTEM}, {"role": "user", "content": user}]})
        r.raise_for_status()
    except httpx.HTTPError as e:
        sys.exit(f"Ollama mapping failed ({url}, model {model}): {e}")
    return json.loads(r.json()["message"]["content"])["entities"]


def ask_llm(anon_model: str, prompt: str, model: str) -> str:
    code = claude(SYSTEM, f"Anonymized model:\n{anon_model}\n\nTask:\n{prompt}", model)
    return re.sub(r"^```(?:python)?\s*|\s*```$", "", code)


# --------------------------------------------------------------------------- #
# Validation + local execution
# --------------------------------------------------------------------------- #

SAFE_BUILTINS = {f.__name__: f for f in (abs, round, min, max, sum, len, float, int, str)}
ALLOWED_NODES = (
    ast.Module, ast.Expr, ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Name, ast.Load, ast.Store,
    ast.Constant, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp, ast.If, ast.Call,
    ast.Attribute, ast.Dict, ast.List, ast.Tuple, ast.JoinedStr, ast.FormattedValue, ast.keyword,
    ast.operator, ast.unaryop, ast.boolop, ast.cmpop, ast.expr_context,
)
MAX_EXPONENT = 100


def validate(code: str, ids: set[str]) -> ast.Module:
    """Allow only straight-line arithmetic over the IDs; raise ValueError otherwise."""
    tree = ast.parse(code)
    assigned = set()
    for node in ast.walk(tree):
        line = getattr(node, "lineno", "?")
        if not isinstance(node, ALLOWED_NODES):
            raise ValueError(f"forbidden construct: {type(node).__name__} (line {line})")
        if isinstance(node, ast.Attribute) and not (
                isinstance(node.value, ast.Name) and node.value.id == "math" and not node.attr.startswith("_")):
            raise ValueError(f"forbidden attribute access (line {line})")
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            raise ValueError(f"forbidden name {node.id} (line {line})")
        if isinstance(node, ast.Call) and not isinstance(node.func, (ast.Name, ast.Attribute)):
            raise ValueError(f"forbidden call form (line {line})")
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow) and not (
                isinstance(node.right, ast.Constant) and isinstance(node.right.value, (int, float))
                and abs(node.right.value) <= MAX_EXPONENT):
            raise ValueError(f"exponent must be a literal number <= {MAX_EXPONENT} (line {line})")
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            assigned.add(node.id)
    if "result" not in assigned:
        raise ValueError("code does not assign `result`")
    if assigned & ids:
        raise ValueError(f"code overwrites input IDs: {sorted(assigned & ids)}")
    return tree


def execute(tree: ast.Module, table: IdTable) -> dict:
    env = {"__builtins__": SAFE_BUILTINS, "math": math, **table.values()}
    exec(compile(tree, "<llm_code>", "exec"), env)
    return env["result"]


# --------------------------------------------------------------------------- #
# De-anonymized standalone script (the single output file)
# --------------------------------------------------------------------------- #

def render_script(code: str, table: IdTable, source: str) -> str:
    code_names = {n.id for n in ast.walk(ast.parse(code)) if isinstance(n, ast.Name)}
    names = {}
    for r in table.records:                     # avoid clashes with the LLM's own variables
        var = r.var
        while var in code_names or var in names.values():
            var += "_in"
        names[r.id] = var
    body = re.sub(r"\b[NT]\d{2,}\b", lambda m: names.get(m.group(), m.group()), code.strip())

    entries = "".join(
        f"    {r.id!r}: {{'term': {r.term!r}, 'var': {names[r.id]!r}, 'value': {r.value!r}, 'unit': {r.unit!r}, "
        f"'entity': {r.entity!r}, 'key': {r.key!r}, 'raw': {r.raw!r}}},\n" for r in table.records)
    width = max((len(v) for v in names.values()), default=0)
    assigns = "\n".join(f"{names[r.id]:<{width}} = ID_TABLE[{r.id!r}]['value']" for r in table.records)
    return (f"# De-anonymized from {source} - CONTAINS SENSITIVE DATA, keep local.\n"
            f"import math\n\n"
            f"# ID conversion table (reload with ccs_anon.IdTable.load)\n"
            f"ID_TABLE = {{\n{entries}}}\n\n"
            f"{assigns}\n\n"
            f"# --- calculation (LLM-generated) ---\n{body}\n\n\n"
            f"if __name__ == '__main__':\n"
            f"    for k, v in result.items():\n"
            f"        print(f'{{k}}: {{v}}')\n")


def write_private(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> dict | None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("model_file", type=Path)
    ap.add_argument("prompt", help="what to calculate")
    ap.add_argument("--public", nargs="*", default=[], help="keys whose values are NOT sensitive")
    ap.add_argument("--code-file", type=Path, help="use this code instead of calling the LLM")
    ap.add_argument("--out", type=Path, help="de-anonymized script (default: <model>.local.py)")
    ap.add_argument("--table", type=Path, action="append", default=[],
                    help="also export the ID table (.json/.csv/.md); repeatable")
    ap.add_argument("--ontology", type=Path, help="ontology file (default: ontology.json next to the script)")
    ap.add_argument("--ontology-mapper", choices=["claude", "ollama", "none"], default="claude",
                    help="who maps unknown names: claude (remote, names only), ollama (local), "
                         "none (ontology file only; unknown names fall back to ASCII)")
    ap.add_argument("--ollama-model", default="Qwen2.5-Coder:7b")
    ap.add_argument("--llm-model", default="claude-opus-5")
    ap.add_argument("--dry-run", action="store_true", help="show what would be sent, write nothing")
    a = ap.parse_args(argv)

    out = a.out or a.model_file.with_name(a.model_file.stem + ".local.py")
    onto_path = a.ontology or ONTOLOGY_PATH
    if any(p.resolve() == a.model_file.resolve() for p in [out, onto_path, *a.table]):
        sys.exit(f"refusing to overwrite the model file {a.model_file}")

    ontology = Ontology(onto_path)
    mapper = None
    if a.ontology_mapper == "ollama":             # local: allowed even in --dry-run
        mapper = lambda req, existing: ollama_map_terms(req, existing, a.ollama_model)  # noqa: E731
    elif a.ontology_mapper == "claude" and not a.dry_run and have_key():
        mapper = lambda req, existing: llm_map_terms(req, existing, a.llm_model)  # noqa: E731
    anon, table = IdTable.from_model(a.model_file.read_text(encoding="utf-8"), set(a.public), ontology, mapper)
    if not a.dry_run:
        ontology.save()
    prompt = table.scrub(a.prompt)
    print(f"== Ontology mapping ({onto_path}) ==\n" + table.mapping_text())
    if any(s == "fallback" for *_, s in table.mapping):
        print("  ! fallback = not in the ontology and not mapped by the LLM (ASCII name, not saved)")
    print("\n== ID conversion table (local only) ==\n" + table.to_text())
    print("\n== Sent to LLM ==\n" + anon.strip() + "\n\nTask: " + prompt)
    if a.dry_run:
        return None

    code = a.code_file.read_text(encoding="utf-8") if a.code_file else ask_llm(anon, prompt, a.llm_model)
    print("\n== Code (IDs only) ==\n" + code.strip())
    result = execute(validate(code, set(table.by_id)), table)

    write_private(out, render_script(code, table, a.model_file.name))
    for p in a.table:
        table.save(p)
    print(f"\n== Saved (mode 600): {', '.join(map(str, [out, *a.table]))}")

    print("\n== Result (computed locally with original values) ==")
    for k, v in result.items():
        shown = f"{v:,.2f}".replace(",", " ") if isinstance(v, (int, float)) else v
        print(f"  {k}: {shown}")
    return result


if __name__ == "__main__":
    main()
