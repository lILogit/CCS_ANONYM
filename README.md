# CCS_ANONYM

Privacy-preserving calculations over CCS models with an LLM. Sensitive values never leave
your machine: the LLM only sees placeholder IDs and English ontology terms, writes the
calculation as Python, and the numbers are filled in and computed locally.

## How it works

```
model.md ─ontology─► English terms ─anonymize─► IDs only ─Claude─► Python over IDs ─validate─► run locally with real values
 (local)               (local)                   (sent)             (received)        (local)      └► model.local.py
```

| Step | Where | What happens |
|---|---|---|
| 1. Ontology | local (+ optional mapper) | Entity and key names are mapped to a controlled English vocabulary in `ontology.json` (`Vehicle.insurance_cost`). Known names resolve locally. Unknown names are mapped once by Claude or a local Ollama model (names and units only, never values) and saved as aliases. |
| 2. Anonymize | local | Numbers become `N01, N02…`, text becomes `T01, T02…`. Real values you type in your question are replaced too (`120 000`, `120.000` and `120000km` all become `N03`). |
| 3. Claude | remote | Gets only the anonymized model and your question, and returns Python code over the IDs. |
| 4. Safety check | local | Only straight-line arithmetic, `if`/`else` and `math.*` are allowed. Imports, files, loops, functions, subscripts and variable exponents are rejected. |
| 5. Run | local | The IDs are bound to your real values, the code runs, and the result is printed. |
| 6. Save | local | `<model>.local.py` is written (mode 600): the ID table, readable variables and the calculation. It runs on its own. |

## Setup

```bash
pip install anthropic          # httpx comes with it
echo "ANTHROPIC_API_KEY=sk-ant-..." > .env && chmod 600 .env
```

A key already set in your environment wins over `.env`. For local ontology mapping, install
[Ollama](https://ollama.com) and pull a model (default `Qwen2.5-Coder:7b`).

## Model file format

```
$ property {type: car  model: Porsche  assurence_cost: 3100 Kč/ monthly  dalnicni_znamka: 2300 Kč/yearly  najeto: 120.000 / km}
$ byt      {mesto: Brno  plocha: 68 m2  najemne: 17 500 Kč/monthly}
```

- Each entity is `$ name { key: value unit  key: value unit … }`; a file can hold several.
- Numbers can use Czech/EU formats: `120.000` and `120 000` mean 120000, and `12,5` means 12.5.
- Whatever follows the number is its unit. Always include the period (`Kč/monthly`,
  `km/yearly`), because the LLM uses it for conversions.
- Names can be Czech, English or misspelled; the ontology normalizes them.
- Limitation: full dates (`15.03.2021`) are read as numbers. Use years or durations.

See [`sample_model.md`](sample_model.md) for a fictional household with 6 entities.

## Usage

```bash
python3 ccs_anon.py <model_file> "<question>" [options]
```

| Option | Meaning |
|---|---|
| `--dry-run` | Show the mapping, the ID table and exactly what would be sent. Nothing is sent to Claude or written. |
| `--ontology-mapper claude\|ollama\|none` | Who maps unknown names: Claude (default; names only), local Ollama (nothing leaves the machine), or none (ASCII fallback, not saved). |
| `--ollama-model NAME` | Ollama model for mapping (default `Qwen2.5-Coder:7b`; host from `OLLAMA_HOST`). |
| `--ontology FILE` | Ontology file (default `ontology.json` next to the script). |
| `--public KEY …` | Keep these keys' values readable, e.g. `--public type`. |
| `--out FILE` | Output script (default `<model>.local.py`). |
| `--table FILE` | Also export the ID table as `.json`, `.csv` or `.md`. Repeatable. |
| `--code-file FILE` | Skip the LLM and use your own code, written with the IDs. |
| `--llm-model ID` | Claude model (default `claude-opus-5`). |

```bash
python3 ccs_anon.py sample_model.md "Monthly budget balance incl. mortgage payment" --dry-run
python3 ccs_anon.py sample_model.md "Monthly budget balance incl. mortgage payment" --ontology-mapper ollama
python3 cc_model.local.py            # re-run a saved calculation, no LLM needed
```

Reuse an ID table in Python:

```python
from pathlib import Path
from ccs_anon import IdTable

t = IdTable.load(Path("sample_model.local.py"))   # also .json / .csv
t.value("N03")                  # 120000.0
t.id_of("Porsche")              # 'T02'
t.deanonymize_text("N01*12")    # '3100.0*12'
```

## Ontology

`ontology.json` holds classes and `Class.property` terms, each with a label, an optional
schema.org equivalent (`same_as`) and aliases (the original names):

```json
"Vehicle.mileage_from_odometer": {
  "label": "Mileage", "same_as": "https://schema.org/mileageFromOdometer", "aliases": ["najeto"]
}
```

Matching ignores accents and case. A generic entity name such as `property` resolves to a
class only if that class has a term for every key, so different kinds of "property" get
different classes. The file holds names only (no values). Review it and edit it by hand as needed.

## Privacy: what leaves your machine

| Sent | Never sent |
|---|---|
| IDs (`N01`, `T02`) | Values |
| English ontology terms and units | The ID table |
| Your question, with known values replaced | `*.local.py`, `.env`, model files |
| Original names, only when mapping new names with `--ontology-mapper claude` | |

- Only values that appear in the model are replaced. Other names or numbers in your
  question are sent as-is, so check with `--dry-run`.
- Output files are mode 600, and the tool refuses to overwrite your model file.
- Keep `.env`, `*.local.py` and your real models out of git (see `.gitignore`).

## Troubleshooting

| Message | Fix |
|---|---|
| `No API key: set ANTHROPIC_API_KEY…` | Fill in `.env`. |
| `Ollama mapping failed …` | Start Ollama (`ollama serve`) or pull the model. |
| `forbidden construct: …` / `exponent must be…` | The LLM broke a safety rule. Run again or rephrase. |
| `code does not assign result` | Run again. |
| `LLM refused: …` | Rephrase the question. |
| `refusing to overwrite the model file` | Choose a different `--out`, `--table` or `--ontology` path. |
| `(fallback)` in the mapping | The name isn't in the ontology and wasn't mapped. Use a mapper or edit `ontology.json`. |

## Tests

```bash
python3 -m pytest -q test_ccs_anon.py      # 66 tests, no API key or Ollama needed (both mocked)
```
