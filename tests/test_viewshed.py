from pathlib import Path

from viewshed_toolkit import STAGES, ViewshedRequest, WorkflowIdentity, load_app_config
from viewshed_toolkit.pipeline.contracts import (
    DEFAULT_WORKFLOW_IDENTITY,
    workflow_identity_from_config,
)
from viewshed_toolkit.resources import common_areas_path, default_config_path


def test_packaged_default_configuration_loads() -> None:
    config = default_config_path()
    app = load_app_config(config)

    assert config.is_file()
    assert common_areas_path().is_file()
    assert app.region.name == "model_area"
    assert app.h3.source_resolution == 7


def test_public_request_uses_registered_stages() -> None:
    request = ViewshedRequest(config=Path("example.yaml"))

    assert request.stages == STAGES


def test_workflow_identity_is_generic_unless_configured() -> None:
    assert workflow_identity_from_config({}) == DEFAULT_WORKFLOW_IDENTITY
    assert workflow_identity_from_config(
        {
            "provenance": {
                "workflow": "example.visibility",
                "dataset_id": "example.visibility.static",
                "producer": "example",
            }
        }
    ) == WorkflowIdentity(
        workflow="example.visibility",
        dataset_id="example.visibility.static",
        producer="example",
    )


def test_packaged_case_study_preserves_orcacast_identity() -> None:
    app = load_app_config(default_config_path())

    assert workflow_identity_from_config(app.raw_config) == WorkflowIdentity(
        workflow="human.viewshed",
        dataset_id="human.viewshed.static",
        producer="human.viewshed",
    )
