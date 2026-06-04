# Data Engineering Conventions
# ~/.claude/rules/data-engineering.md
# Merged from: python.md + data.md + notebooks.md

## Python Style

- Use f-strings for string formatting (never `.format()` or `%`)
- Type hints on function signatures (args + return), not on local variables
- Import order: stdlib > third-party > local (one blank line between groups)
- No bare `except:` — always catch specific exceptions
- Prefer `pathlib.Path` over `os.path` for file operations

## DuckDB

- Always use `.fetchdf()` to get pandas DataFrame from DuckDB results
- Set memory/thread limits explicitly on every connection
- Use `preserve_insertion_order=false` for performance
- Close connections explicitly with `con.close()`
- Register DataFrames before SQL queries: `con.register("name", df)`

## Data Patterns

- Always use `SEED` constant for reproducibility in random/numpy/faker
- Use `datetime.now(UTC).isoformat()` for timestamps (not `.now()` without timezone, not deprecated `utcnow()`)
- Parquet for local I/O, Delta for Spark/Fabric
- `mode="overwrite"` + `overwriteSchema=true` for Delta writes (unless appending)

## Medallion Architecture

All data projects follow Bronze > Silver > Gold:

| Layer | Purpose | Naming |
|-------|---------|--------|
| Bronze | Raw source records | `bronze_<source_system>` |
| Silver | Deduplicated golden records | `silver_<entity>` |
| Gold | Features, segments, scores | `gold_<domain>_<artifact>` |

## Config Structure

Every data project needs two config files at `config/`:

- `pipeline_config.py` — MODE, SEED, volumes, ML hyperparams, segmentation boundaries
- `fabric_config.py` — LAKEHOUSE_NAME, table constants, IS_FABRIC detection, helpers

## Guardrails

- Never hardcode lakehouse paths — use config constants
- Never assume Spark is available — always provide DuckDB/local fallback
- Always detect environment: `IS_FABRIC = os.path.exists("/lakehouse/default/Files")`
- Use `resolve_file_path()` for any file I/O in Fabric (handles ABFSS + local)
- Synthetic data must use `SEED` for reproducibility
- Foreign keys must reference parent entities (never orphaned IDs)

## ML Conventions

- Standard model battery: Logistic Regression (baseline), Random Forest, XGBoost, LightGBM
- Compare on AUC, F1, precision, recall — pick best by AUC
- Batch predictions must include `model_version` and `scored_at` columns
- RFM segmentation uses quintile-based scoring

## Delta Table Writes

```python
# Always: overwrite + overwriteSchema
sdf.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(table_name)
```

## Local Fallback

```python
# When Spark unavailable
df.to_parquet(path, index=False)  # write
df = pd.read_parquet(path)        # read
```

---

## Jupyter Notebooks

### Execution Order

Strict sequential — each notebook depends on the prior:
```
NB_A (ingest + resolve) -> NB_B (features + ML) -> NB_C (dashboard)
```

Never skip or reorder notebooks in a pipeline.

### Cell Structure

1. Cell 1: `%pip install` dependencies (pinned versions)
2. Cell 2: imports + environment validation
3. Cell 3+: pipeline logic with markdown section headers between logical blocks

### Rules

- Every code cell should be idempotent (safe to re-run)
- No hardcoded paths — use config constants
- Markdown cells as section headers before each logical block
- Output cells should be cleared before commit (unless they're the deliverable)
- Keep cells focused — one logical operation per cell

### Version Pins

- Always pin critical dependencies (e.g., `splink==3.9.15`, `sqlglot<26`)
- Document WHY a pin exists in a comment next to it
- Environment files: `environment.yml` (conda) or `requirements.txt` (pip)

### Local Runner (`run_local.py`)

When a notebook targets Fabric/Databricks, provide a local runner that:
- Skips `%pip` cells
- Stubs `pyspark` imports
- Replaces `notebookutils` with local fallbacks
- Replaces `tqdm.notebook` with `tqdm.auto`

---

## Never

- `from module import *`
- Mutable default arguments (`def f(x=[])`)
- Hardcoded file paths — use config constants or `resolve_file_path()`
- Commit `.env`, credentials, or connection strings
- Ignore `SettingWithCopyWarning` — fix with `.copy()` or `.loc[]`
