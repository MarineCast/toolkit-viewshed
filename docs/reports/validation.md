# Validation status

Migration validation checks:

- land and water pair-key uniqueness and nullness;
- finite static weights bounded to `[0, 1]`;
- artifact checksum agreement with metadata;
- configuration and scientific-hash alignment;
- notebook JSON integrity and absence of embedded error outputs;
- byte size and SHA-256 for every ignored case-study artifact.

Run it with:

```bash
PYTHONPATH=src python scripts/validate_copied_outputs.py \
  --output docs/reports/viewshed_migration_validation.json
```

The complete automated suite validates focused configuration, geometry, sampling, raster,
terrain, aggregation, finalization, mapping, and service contracts. Neither this report nor the
tests replace a full regional rebuild or notebook execution against all upstream OrcaCast data.
The organized migration and contract-layer refactor pass all 200 tests in the configured
OrcaCast Python environment using the ordinary `PYTHONPATH=src` package layout.
