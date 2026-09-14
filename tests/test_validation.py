from viewshed_toolkit import validate as validate_artifacts
from viewshed_toolkit._internal.geo.raster import validate_raster_grid_alignment


def test_canonical_validation_imports_are_callable() -> None:
    assert callable(validate_artifacts)
    assert callable(validate_raster_grid_alignment)
