"""No-network routing checks prevent the supported demo command undoing documentation."""

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def runner(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    module = importlib.import_module("run_san_juan_demo")
    app = SimpleNamespace(
        config_path=tmp_path / "config.yaml",
        raw_config={},
        config_hash="test",
        paths=SimpleNamespace(final_output_dir=tmp_path / "outputs"),
    )
    calls = []
    monkeypatch.setattr(module, "load_app_config", lambda _path: app)
    monkeypatch.setattr(module, "resolve_path", lambda *_args: tmp_path / "data")
    monkeypatch.setattr(module, "component_root", lambda _app: tmp_path / "outputs/components")
    for boundary in (
        "prepare_case_geometry",
        "prepare_canopy_windows",
        "run_component_stage",
        "check_inputs",
        "process",
    ):
        monkeypatch.setattr(
            module, boundary, lambda *_args, boundary=boundary, **_kwargs: calls.append(boundary)
        )

    def forbidden(*args, **kwargs):
        raise RuntimeError("retired documentation writer called")

    monkeypatch.setattr(module, "plot_demo", forbidden, raising=False)
    return module, calls


@pytest.mark.parametrize("flags", [[], ["--model-only"]])
def test_demo_default_routes_only_to_model(runner, monkeypatch, flags, capsys):
    module, calls = runner
    monkeypatch.setattr("sys.argv", ["run_san_juan_demo.py", *flags])
    assert module.main() is None
    assert calls.count("process") == 1
    assert "build_documentation_examples.py" in capsys.readouterr().out


@pytest.mark.parametrize(
    "flags", [["--model-only", "--render-only"], ["--model-only", "--prepare-only"]]
)
def test_demo_rejects_ambiguous_modes(runner, monkeypatch, flags):
    module, calls = runner
    monkeypatch.setattr("sys.argv", ["run_san_juan_demo.py", *flags])
    with pytest.raises(SystemExit) as failure:
        module.main()
    assert failure.value.code == 2
    assert calls == []


def test_render_only_has_migration_error(runner, monkeypatch, capsys):
    module, calls = runner
    monkeypatch.setattr("sys.argv", ["run_san_juan_demo.py", "--render-only"])
    with pytest.raises(SystemExit):
        module.main()
    assert "build_documentation_examples.py" in capsys.readouterr().err
    assert calls == []


@pytest.mark.parametrize(
    "directory", ["docs", "docs/assets", "docs/assets/san-juan-demo.html", ".github"]
)
def test_legacy_render_rejects_tracked_destinations_before_any_producer(runner, directory):
    module, calls = runner
    with pytest.raises(SystemExit) as failure:
        module.main(["--legacy-render", "--legacy-output", str(ROOT / directory)])
    assert failure.value.code == 2
    assert calls == []


def test_rebuild_model_only_does_not_write_documentation(runner, monkeypatch):
    import viewshed_toolkit.pipeline.api.stages as stages

    module, calls = runner
    monkeypatch.setattr(stages, "run_stage", lambda *_args: calls.append("run_stage"))
    paths = [
        ROOT / "docs/assets/san-juan-demo.html",
        *sorted((ROOT / "docs/assets/examples/san-juan").rglob("*")),
    ]
    before = {path: path.read_bytes() for path in paths if path.is_file()}
    assert module.main(["--rebuild", "--model-only"]) is None
    assert "run_stage" in calls and "process" not in calls
    assert all(path.read_bytes() == content for path, content in before.items())


@pytest.mark.parametrize(
    "flags",
    [
        ["--legacy-output", "work/tmp"],
        ["--legacy-render", "--rebuild"],
        ["--legacy-render", "--model-only"],
    ],
)
def test_legacy_flags_cannot_implicitly_run_or_export(runner, flags):
    module, calls = runner
    with pytest.raises(SystemExit) as failure:
        module.main(flags)
    assert failure.value.code == 2
    assert calls == []


def test_readme_and_documentation_links_resolve(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    checker = importlib.import_module("check_documentation_links")
    assert checker.check()["local_references_checked"] > 100


def test_prepare_only_returns_before_model(runner):
    module, calls = runner
    assert module.main(["--prepare-only"]) is None
    assert "check_inputs" in calls and "process" not in calls


def test_prepare_and_rebuild_error_before_producers(runner):
    module, calls = runner
    with pytest.raises(SystemExit) as failure:
        module.main(["--prepare-only", "--rebuild"])
    assert failure.value.code == 2
    assert calls == []
