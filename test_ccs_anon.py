"""Tests for ccs_anon.py - run with:  python3 -m pytest -v test_ccs_anon.py"""
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import ccs_anon as ca

HERE = Path(__file__).parent
# fictional demo model (cc_model.md stays local and is git-ignored)
SAMPLE = "$ property {type: car  model: Porsche  assurence_cost: 3100 Kč/ monthly  dalnicni_znamka: 2300 Kč/yearly  najeto: 120.000 / km}"
SECRETS = ["Porsche", "3100", "2300", "120.000", "120000", "car"]

LLM_CODE = """\
insurance_yearly = N01 * 12   # monthly -> yearly
total = insurance_yearly + N02
result = {"total": total, "per_km": total / N03, "label": T02}
"""


FAKE_TERMS = {"type": "vehicle_type", "model": "model_name", "assurence_cost": "insurance_premium",
              "dalnicni_znamka": "highway_vignette_fee", "najeto": "odometer_reading"}


@pytest.fixture(autouse=True)
def ontology_path(monkeypatch, tmp_path_factory):
    """Never touch the real ontology.json next to the script."""
    p = tmp_path_factory.mktemp("onto") / "ontology.json"
    monkeypatch.setattr(ca, "ONTOLOGY_PATH", p)
    return p


@pytest.fixture
def model(tmp_path):
    p = tmp_path / "cc_model.md"
    p.write_text(SAMPLE, encoding="utf-8")
    return p


@pytest.fixture
def table():
    return ca.IdTable.from_model(SAMPLE)[1]


# --- 1. parsing ------------------------------------------------------------ #

@pytest.mark.parametrize("s,expected", [
    ("3100", 3100.0), ("120.000", 120000.0), ("1 234 567", 1234567.0),
    ("1 234,5", 1234.5), ("12,5", 12.5), ("1.5", 1.5), ("-42", -42.0),
])
def test_parse_number(s, expected):
    assert ca.parse_number(s) == expected


def test_parse_model_keys(table):
    assert [r.key for r in table.records] == ["type", "model", "assurence_cost", "dalnicni_znamka", "najeto"]


def test_multiple_entities():
    text = "$ car {price: 1 200 000 Kč}\n$ house {rent: 25.000 Kč/monthly owner: Novak}"
    anon, t = ca.IdTable.from_model(text)
    assert [(r.id, r.entity, r.value) for r in t.records] == [
        ("N01", "car", 1200000.0), ("N02", "house", 25000.0), ("T01", "house", "Novak")]
    assert "Novak" not in anon and "25.000" not in anon


# --- 2. anonymization ------------------------------------------------------ #

def test_anonymize_values_and_units(table):
    assert table.values() == {"T01": "car", "T02": "Porsche", "N01": 3100.0, "N02": 2300.0, "N03": 120000.0}
    assert table.by_id["N01"].unit == "Kč/monthly"
    assert table.by_id["N02"].unit == "Kč/yearly"


def test_anonymized_text_has_no_secrets():
    anon, _ = ca.IdTable.from_model(SAMPLE)
    for s in SECRETS:
        assert s not in anon, s
    assert "assurence_cost: N01 [Kč/monthly]" in anon


def test_public_keys_stay_clear():
    anon, t = ca.IdTable.from_model(SAMPLE, {"type"})
    assert "type: car" in anon
    assert "type" not in {r.key for r in t.records}


def test_scrub_prompt(table):
    assert table.scrub("Cost of my porsche with 3100 insurance") == "Cost of my T02 with N01 insurance"


@pytest.mark.parametrize("prompt,expected", [
    ("drove 120 000 km, 120.000 km, 120000km", "drove N03 km, N03 km, N03km"),
    ("pay 3100/month and 2 300 yearly, 12 months", "pay N01/month and N02 yearly, 12 months"),
    ("price 31000 or 3100.5", "price 31000 or 3100.5"),
])
def test_scrub_prompt_number_formats(table, prompt, expected):
    assert table.scrub(prompt) == expected


def test_scrub_prompt_whole_words_only(table):
    # 'car' is a value (T01) but must not corrupt ordinary words containing it
    assert table.scrub("calculate carefully for the car") == "calculate carefully for the T01"


# --- 3. ID table ----------------------------------------------------------- #

@pytest.mark.parametrize("fmt", ["json", "csv"])
def test_table_roundtrip(table, tmp_path, fmt):
    p = table.save(tmp_path / f"t.{fmt}")
    assert p.stat().st_mode & 0o777 == 0o600
    loaded = ca.IdTable.load(p)
    assert loaded.rows() == table.rows()


def test_table_markdown(table, tmp_path):
    text = table.save(tmp_path / "t.md").read_text(encoding="utf-8")
    assert "| N03 | property | najeto | Property.najeto | najeto | 120000.0 | /km | 120.000 / km |" in text


def test_optional_table_exports(monkeypatch, model, fake_claude):
    run_main(monkeypatch, model, "x", "--table", model.parent / "t.csv", "--table", model.parent / "t.md")
    assert {f.name for f in model.parent.iterdir()} == {"cc_model.md", "cc_model.local.py", "t.csv", "t.md"}


def test_table_bad_format(table, tmp_path):
    with pytest.raises(ValueError):
        table.save(tmp_path / "t.xlsx")


def test_table_lookups(table):
    assert table.value("N03") == 120000.0
    assert table.id_of("Porsche") == "T02"
    assert table.id_of(3100.0) == "N01"
    assert table.id_of("nope") is None
    assert table.deanonymize_text("N01*12 + N02", use="var") == "assurence_cost*12 + dalnicni_znamka"
    assert table.deanonymize_text("N01*12") == "3100.0*12"
    assert table.by_id["T01"].var == "property_type"          # 'type' is a builtin -> prefixed


# --- 4. validation of LLM code --------------------------------------------- #

@pytest.mark.parametrize("bad", [
    "import os\nresult = {}",
    "from os import path\nresult = {}",
    "result = {'x': open('/etc/passwd').read()}",
    "result = {'x': __import__('os')}",
    "result = {'x': N01.__class__}",
    "result = {'x': math.__dict__}",
    "result = {'x': (lambda: 1)()}",
    "while True:\n    pass\nresult = {}",
    "def f():\n    return 1\nresult = {}",
    "result = {'x': [c for c in 'ab']}",
    "N01 = 0\nresult = {}",
    "x = 1",
    "result = {'x': 10 ** 10 ** 10}",
    "result = {'x': N01 ** N02}",
    "result = {'x': [N01][0]}",
])
def test_validate_rejects(bad, table):
    with pytest.raises((ValueError, SyntaxError)):
        ca.validate(bad, set(table.by_id))


def test_validate_accepts(table):
    ca.validate(LLM_CODE, set(table.by_id))
    ca.validate("x = math.sqrt(N01) if N01 > 0 else 0\nresult = {'x': round(x, 2)}", set(table.by_id))


def test_exec_has_no_real_builtins(table):
    # even if validation were bypassed, dangerous builtins are not reachable
    tree = ca.ast.parse("result = {'x': open}")
    with pytest.raises(NameError):
        ca.execute(tree, table)


# --- 5. local execution ---------------------------------------------------- #

def test_execute(table):
    r = ca.execute(ca.validate(LLM_CODE, set(table.by_id)), table)
    assert r == {"total": 3100 * 12 + 2300, "per_km": (3100 * 12 + 2300) / 120000, "label": "Porsche"}


# --- 6. de-anonymized code ------------------------------------------------- #

def test_deanonymized_code_runs_standalone(table, tmp_path):
    code = ca.render_script(LLM_CODE, table, "cc_model.md")
    assert "N01" not in code.split("# --- calculation")[1]
    assert "ID_TABLE = {" in code
    p = tmp_path / "d.py"
    p.write_text(code, encoding="utf-8")
    ns = {}
    exec(compile(code, str(p), "exec"), ns)
    assert ns["result"] == ca.execute(ca.validate(LLM_CODE, set(table.by_id)), table)
    assert ns["ID_TABLE"]["N03"]["value"] == 120000.0
    out = subprocess.run([sys.executable, str(p)], capture_output=True, text=True, check=True).stdout
    assert "total: 39500.0" in out


def test_deanonymize_avoids_name_clash(table):
    code = "najeto = N03 / 2\nresult = {'half': najeto}"   # LLM reuses a key name
    d = ca.render_script(code, table, "x")
    ns = {}
    exec(d, ns)
    assert ns["result"] == {"half": 60000.0}


# --- 7. end-to-end with mocked Claude -------------------------------------- #

class FakeMessages:
    def __init__(self, sent):
        self.sent = sent

    def create(self, **kw):
        self.sent.append(kw)
        if "output_config" in kw:                     # ontology mapping request
            req = json.loads(kw["messages"][0]["content"])["entities"]
            text = json.dumps({"entities": [
                {"ref": e["ref"], "class": "Vehicle", "class_label": "Vehicle",
                 "class_same_as": "https://schema.org/Vehicle",
                 "keys": [{"key": k, "property": FAKE_TERMS.get(k, k), "label": k, "same_as": ""}
                          for k in e["keys"]]} for e in req]})
        else:
            text = "```python\n" + LLM_CODE + "```"
        return SimpleNamespace(stop_reason="end_turn", stop_details=None,
                               content=[SimpleNamespace(type="thinking", text=""),
                                        SimpleNamespace(type="text", text=text)])


@pytest.fixture
def fake_claude(monkeypatch):
    import anthropic
    sent = []
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(anthropic, "Anthropic",
                        lambda: SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages(sent))))
    return sent


def run_main(monkeypatch, *argv):
    return ca.main([str(a) for a in argv])


def test_end_to_end(monkeypatch, capsys, model, fake_claude, ontology_path):
    before = model.read_bytes()
    run_main(monkeypatch, model, "Yearly cost of my Porsche, insurance is 3100")
    out = capsys.readouterr().out

    # two requests (ontology mapping + calculation), neither carries a sensitive value
    assert len(fake_claude) == 2
    for req in fake_claude:
        payload = json.dumps(req, ensure_ascii=False, default=str)
        for s in SECRETS:
            assert s not in payload, f"leaked {s!r}"
    calc = json.dumps(fake_claude[1], ensure_ascii=False, default=str)
    assert "T02" in calc and "N01" in calc
    assert "$ Vehicle {" in calc and "insurance_premium: N01 [Kč/monthly]" in calc
    assert "assurence_cost" not in calc and "dalnicni_znamka" not in calc   # English terms only
    assert "Vehicle.insurance_premium" in out and "(learned)" in out

    # result computed locally, with original values
    assert "total: 39 500.00" in out
    assert "label: Porsche" in out

    # local artifacts
    assert model.read_bytes() == before, "model file must never be modified"
    assert sorted(f.name for f in model.parent.iterdir()) == ["cc_model.local.py", "cc_model.md"]
    assert (model.parent / "cc_model.local.py").stat().st_mode & 0o777 == 0o600
    expected = ca.IdTable.from_model(SAMPLE, ontology=ca.Ontology(ontology_path))[1].rows()
    assert ca.IdTable.load(model.parent / "cc_model.local.py").rows() == expected

    # ontology learned; second run resolves locally -> only the calculation request
    onto = json.loads(ontology_path.read_text(encoding="utf-8"))
    assert "property" in onto["classes"]["Vehicle"]["aliases"]
    assert onto["terms"]["Vehicle.highway_vignette_fee"]["aliases"] == ["dalnicni_znamka"]
    fake_claude.clear()
    run_main(monkeypatch, model, "again")
    assert len(fake_claude) == 1 and "output_config" not in fake_claude[0]
    assert "(known)" in capsys.readouterr().out


def test_dry_run_does_not_call_llm(monkeypatch, capsys, model, fake_claude):
    run_main(monkeypatch, model, "x", "--dry-run")
    assert fake_claude == []
    assert [f.name for f in model.parent.iterdir()] == ["cc_model.md"]
    assert "== Sent to LLM ==" in capsys.readouterr().out


def test_refuses_to_overwrite_model(monkeypatch, model, fake_claude):
    before = model.read_bytes()
    with pytest.raises(SystemExit):
        run_main(monkeypatch, model, "x", "--table", model, "--dry-run")
    with pytest.raises(SystemExit):
        run_main(monkeypatch, model, "x", "--out", model, "--dry-run")
    assert model.read_bytes() == before


def test_llm_refusal(monkeypatch, model):
    import anthropic

    class Refusing:
        def create(self, **kw):
            return SimpleNamespace(stop_reason="refusal", stop_details={"category": "x"}, content=[])

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(anthropic, "Anthropic",
                        lambda: SimpleNamespace(beta=SimpleNamespace(messages=Refusing())))
    with pytest.raises(SystemExit):
        run_main(monkeypatch, model, "x")


# --- 8. .env loading -------------------------------------------------------- #

def test_load_env(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# comment\n\nexport A_KEY="sk-1"\nB_KEY=\'x=y\'\nEMPTY=\nPRESET=from-file\n')
    for k in ("A_KEY", "B_KEY", "EMPTY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PRESET", "from-env")
    ca.load_env(env)
    import os
    assert os.environ["A_KEY"] == "sk-1"
    assert os.environ["B_KEY"] == "x=y"
    assert "EMPTY" not in os.environ
    assert os.environ["PRESET"] == "from-env"      # real environment wins
    ca.load_env(tmp_path / "missing.env")           # no error


def test_missing_key_exits(monkeypatch, model, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(ca, "load_env", lambda *a: None)
    with pytest.raises(SystemExit, match="No API key"):
        run_main(monkeypatch, model, "x")


# --- 9. ontology ------------------------------------------------------------ #

def fake_mapper(calls, cls="Vehicle", terms=FAKE_TERMS):
    def mapper(request, existing):
        calls.append((request, existing))
        return [{"ref": e["ref"], "class": cls, "class_label": cls, "class_same_as": "",
                 "keys": [{"key": k, "property": terms.get(k, k), "label": k, "same_as": ""} for k in e["keys"]]}
                for e in request]
    return mapper


@pytest.mark.parametrize("s,expected", [
    ("dalnicni_znamka", "dalnicni_znamka"), ("Dálniční známka", "dalnicni_znamka"),
    ("PojištěníAuta", "pojisteniauta"), ("2nd-owner", "f_2nd_owner"), ("", "f_"),
])
def test_slug(s, expected):
    assert ca.slug(s) == expected


def test_learn_save_reload(ontology_path):
    calls = []
    onto = ca.Ontology(ontology_path)
    anon, t = ca.IdTable.from_model(SAMPLE, ontology=onto, mapper=fake_mapper(calls))
    onto.save()
    assert len(calls) == 1
    assert [r.term for r in t.records] == ["Vehicle.vehicle_type", "Vehicle.model_name", "Vehicle.insurance_premium",
                                           "Vehicle.highway_vignette_fee", "Vehicle.odometer_reading"]
    assert [r.var for r in t.records] == ["vehicle_type", "model_name", "insurance_premium",
                                          "highway_vignette_fee", "odometer_reading"]
    assert anon.startswith("$ Vehicle {vehicle_type: T01")
    assert ontology_path.read_text(encoding="utf-8").startswith('{\n  "format": "ccs-ontology/1"')

    calls.clear()
    anon2, t2 = ca.IdTable.from_model(SAMPLE, ontology=ca.Ontology(ontology_path), mapper=fake_mapper(calls))
    assert calls == [] and anon2 == anon and t2.rows() == t.rows()          # stable, local
    assert {s for *_, s in t2.mapping} == {"known"}


def test_mapper_request_has_no_values(ontology_path):
    calls = []
    ca.IdTable.from_model(SAMPLE, ontology=ca.Ontology(ontology_path), mapper=fake_mapper(calls))
    sent = json.dumps(calls, ensure_ascii=False)
    for s in SECRETS:
        assert s not in sent.replace('"cars"', ""), s
    assert '"units": {"assurence_cost": "Kč/monthly"' in sent


def test_aliases_are_diacritic_and_case_insensitive(ontology_path):
    onto = ca.Ontology(ontology_path)
    ca.IdTable.from_model(SAMPLE, ontology=onto, mapper=fake_mapper([]))
    text = "$ Property {Dálniční_Známka: 1 000 Kč/yearly  NAJETO: 5 km}"
    _, t = ca.IdTable.from_model(text, ontology=onto)
    assert [r.term for r in t.records] == ["Vehicle.highway_vignette_fee", "Vehicle.odometer_reading"]


def test_same_entity_name_different_meaning(ontology_path):
    """'property' learned as Vehicle must not swallow a house described with other keys."""
    onto = ca.Ontology(ontology_path)
    ca.IdTable.from_model(SAMPLE, ontology=onto, mapper=fake_mapper([]))
    calls = []
    _, t = ca.IdTable.from_model("$ property {najemne: 25.000 Kč/monthly}", ontology=onto,
                                 mapper=fake_mapper(calls, "RealEstate", {"najemne": "monthly_rent"}))
    assert len(calls) == 1
    assert "Vehicle" in calls[0][1]["classes"]                     # existing ontology offered for reuse
    assert t.records[0].term == "RealEstate.monthly_rent"
    assert sorted(onto.classes) == ["RealEstate", "Vehicle"]


def test_invalid_mapper_answer_falls_back(ontology_path):
    bad = [
        lambda req, ex: [],                                                   # no answer
        lambda req, ex: [{"ref": 0, "class": "vehicle", "class_label": "", "class_same_as": "",
                          "keys": [{"key": k, "property": k, "label": "", "same_as": ""} for k in req[0]["keys"]]}],
        lambda req, ex: [{"ref": 0, "class": "Vehicle", "class_label": "", "class_same_as": "",
                          "keys": [{"key": "type", "property": "Bad-Name", "label": "", "same_as": ""}]}],
    ]
    for mapper in bad:
        onto = ca.Ontology(ontology_path)
        _, t = ca.IdTable.from_model(SAMPLE, ontology=onto, mapper=mapper)
        assert {s for *_, s in t.mapping} == {"fallback"}
        assert t.records[2].term == "Property.assurence_cost"
        assert not onto.dirty                                               # fallbacks are never saved


def test_duplicate_properties_are_made_unique(ontology_path):
    same = {"a": "cost", "b": "cost", "c": "cost"}
    _, t = ca.IdTable.from_model("$ x {a: 1 b: 2 c: 3}", ontology=ca.Ontology(ontology_path),
                                 mapper=fake_mapper([], "Thing", same))
    assert [r.term for r in t.records] == ["Thing.cost", "Thing.cost_2", "Thing.cost_3"]


def test_public_key_translated_value_kept(ontology_path):
    anon, t = ca.IdTable.from_model(SAMPLE, {"type"}, ca.Ontology(ontology_path), fake_mapper([]))
    assert "vehicle_type: car" in anon
    assert ("property", "type", "Vehicle.vehicle_type", "learned") in t.mapping
    assert "type" not in {r.key for r in t.records}


def test_ontology_mapper_none(monkeypatch, capsys, model, fake_claude, ontology_path):
    run_main(monkeypatch, model, "x", "--ontology-mapper", "none")
    assert len(fake_claude) == 1 and "output_config" not in fake_claude[0]
    assert "(fallback)" in capsys.readouterr().out
    assert not ontology_path.exists()


def test_dry_run_writes_no_ontology(monkeypatch, model, fake_claude, ontology_path):
    run_main(monkeypatch, model, "x", "--dry-run")
    assert fake_claude == [] and not ontology_path.exists()


def test_old_local_py_without_term_loads(tmp_path):
    p = tmp_path / "old.local.py"
    p.write_text("ID_TABLE = {'N01': {'var': 'a', 'value': 1.0, 'unit': '', 'entity': 'e', 'key': 'a', 'raw': '1'}}\n")
    assert ca.IdTable.load(p).records[0].term == ""


def test_ollama_mapper(monkeypatch, capsys, model, ontology_path):
    import httpx
    sent = []

    def fake_post(url, json=None, timeout=None):
        sent.append((url, json))
        req = __import__("json").loads(json["messages"][1]["content"])["entities"]
        answer = {"entities": [{"ref": e["ref"], "class": "Vehicle", "class_label": "Vehicle", "class_same_as": "",
                                "keys": [{"key": k, "property": FAKE_TERMS[k], "label": k, "same_as": ""}
                                         for k in e["keys"]]} for e in req]}
        return SimpleNamespace(raise_for_status=lambda: None,
                               json=lambda: {"message": {"content": __import__("json").dumps(answer)}})

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:9999")
    run_main(monkeypatch, model, "x", "--ontology-mapper", "ollama", "--dry-run")
    (url, body), = sent
    assert url == "http://127.0.0.1:9999/api/chat"
    assert body["model"] == "Qwen2.5-Coder:7b" and body["format"] == ca.ONTOLOGY_SCHEMA
    assert "Vehicle.insurance_premium  (learned)" in capsys.readouterr().out
    assert not ontology_path.exists()                    # dry-run: learned but not saved

    def down(*a, **k):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(httpx, "post", down)
    with pytest.raises(SystemExit, match="Ollama mapping failed"):
        run_main(monkeypatch, model, "x", "--ontology-mapper", "ollama")


# --- 10. sample model -------------------------------------------------------- #

def test_sample_model_parses():
    text = (HERE / "sample_model.md").read_text(encoding="utf-8")
    anon, t = ca.IdTable.from_model(text)
    assert [e for e in dict.fromkeys(r.entity for r in t.records)] == \
        ["property", "auto", "byt", "hypoteka", "prijem", "sporeni"]
    assert len(t.records) == 27
    by_key = {r.key: (r.value, r.unit) for r in t.records}
    assert by_key["jistina"] == (3200000.0, "Kč")
    assert by_key["zustatek"] == (412350.5, "Kč")
    assert by_key["spotreba"] == (6.2, "l/100km")
    assert by_key["urokova_sazba"] == (4.89, "%/yearly")
    assert by_key["zamestnavatel"] == ("ACME s.r.o.", "")
    for r in t.records:
        assert r.raw not in anon, r.raw
