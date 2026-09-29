# CCS_ANONYM

Privacy-preserving calculations over CCS models (`$ entity {key: value unit …}` files).
Values are anonymized to IDs locally, an LLM writes Python over the IDs, and the code
runs locally with the real values. Everything lives in `ccs_anon.py` (single module,
stdlib + `anthropic` + `httpx`).

## Files

| File | Role | Sensitive |
|---|---|---|
| `ccs_anon.py` | the whole framework + CLI | no |
| `test_ccs_anon.py` | pytest suite, embeds a fictional model (LLM and Ollama mocked, no network) | no |
| `ontology.json` | learned vocabulary: classes, `Class.property` terms, aliases (names only) | no, but review by hand |
| `cc_model.md` | your real input model - local only, git-ignored | **yes** |
| `sample_model.md` | fictional demo model (published) | no |
| `*.local.py` | output: ID_TABLE + calculation with real values (mode 600) | **yes** |
| `.env` | `ANTHROPIC_API_KEY` | **yes, secret** |
| `graphify-out/` | code graph (built from a copy of `ccs_anon.py` only) | no |

## Commands

```bash
python3 -m pytest -q test_ccs_anon.py                          # must stay green, no API key needed
python3 ccs_anon.py cc_model.md "question" --dry-run           # safe preview: no network, writes nothing
python3 ccs_anon.py cc_model.md "question" --ontology-mapper none|ollama|claude
```

## Privacy rules (non-negotiable)

- Never read, print, or `cat` `.env`. To check whether the key is set, count matches of
  `ANTHROPIC_API_KEY=\S+` without printing the value.
- Never send real values anywhere (API calls, subagents, tools like graphify, docs,
  artifacts). Only IDs (`N01`, `T01`), English ontology terms, units and the scrubbed
  question may go to Claude. The ontology mapper gets names and units only.
- Do not feed model files, `*.local.py` or `.env` to graphify or other extractors: copy
  `ccs_anon.py` to a scratch dir and run on that.
- Any new outbound payload needs a test asserting that none of `SECRETS` appears in it
  (see `test_end_to_end`, `test_mapper_request_has_no_values`).
- Sensitive outputs are written with mode 600 (`write_private`, `IdTable.save`).

## Architecture (in pipeline order, driven by `main()`)

1. **Ontology** (`Ontology.resolve`): exact alias match (`slug`: ASCII, no accents or case)
   → unknown names go to the mapper (`llm_map_terms` for Claude with a JSON schema, or
   `ollama_map_terms` locally) → `_valid()` → `learn()` → saved to `ontology.json`.
   An invalid or missing answer gives a `fallback` (ASCII name, never saved). An entity
   resolves to a class only if that class has a term for *every* key, so a generic name
   like `property` can map to different classes.
2. **Anonymize** (`IdTable.from_model`): numbers → `N01…`, text → `T01…`, keys → ontology
   properties; `--public` keys keep their values. `IdTable.scrub` replaces values the user
   typed in the question (numbers compared by value, text on whole words).
3. **LLM** (`ask_llm` → `claude()`): all Claude calls go through `claude()`.
4. **Validate** (`validate`): AST allowlist, straight-line code only; `**` needs a literal
   exponent ≤ 100; no subscripts, loops, functions, imports or dunders. `execute` runs with
   `SAFE_BUILTINS` only.
5. **Output** (`render_script`): the one output file `<model>.local.py`; `IdTable.load`
   reads it back (also `.json`/`.csv`).

## Conventions and gotchas

- Keep it one module; the user prefers **fewer files**. Optional exports (`--table`) stay opt-in.
- **Never overwrite the model file.** A `with_suffix` bug once replaced `cc_model.md`
  (restored from VS Code local history). Build output paths with `with_name(stem + …)`,
  and keep the overwrite guard in `main()` covering every output path.
- Tests must never touch the real `ontology.json`: the autouse `ontology_path` fixture
  patches `ca.ONTOLOGY_PATH`. New Claude calls must be handled in `FakeMessages.create`.
- Anthropic SDK is 0.86: the server-side fallback is passed as
  `betas=["server-side-fallback-2026-07-01"]` + `extra_body={"fallbacks": "default"}`
  (no typed kwarg yet). Structured output uses
  `output_config={"format": {"type": "json_schema", "schema": …}}`. Default model: `claude-opus-5`.
- Ollama: default `Qwen2.5-Coder:7b` at `OLLAMA_HOST` (default `localhost:11434`),
  temperature 0, schema via `format`. On CPU it takes about 80 s per mapping. It works well
  when extending an existing ontology but picks wrong classes from an empty one.
- Known parser limitation: dates like `15.03.2021` parse as the number `15.03`. Use years
  or durations until a date parser exists.
- Numbers use Czech/EU formats: `250.000` / `250 000` = 250000, `12,5` = 12.5; the text
  after the number is the unit (`Kč/monthly`); units always include the period.
