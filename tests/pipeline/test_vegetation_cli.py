import pytest

from viewshed_toolkit.pipeline.prepare.vegetation.cli import main


def test_landcover_cli_rejects_unimplemented_methods():
    with pytest.raises(SystemExit) as exc_info:
        main(["landcover", "--method", "local", "--dry-run"])

    assert exc_info.value.code == 2
