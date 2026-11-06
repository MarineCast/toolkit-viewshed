"""Containment-safe cleanup for viewshed working artifacts."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from viewshed_toolkit._internal.data.parquet import validate_parquet_schema

from ..config.paths import metadata_sidecar_candidates
from ..config.paths import resolve_existing_or_relative_path, resolve_path
from .artifacts import (
    FINAL_SCHEMAS,
    FinalArtifactPaths,
    final_artifact_paths,
    normalize_source_type,
)


def cleanup_policy_from_raw(raw: Mapping[str, Any]) -> str:
    policy = (raw.get("run", {}) or {}).get("cleanup_policy", "retain_reusable")
    if not isinstance(policy, str) or policy not in {"retain_reusable", "compact"}:
        raise ValueError("run.cleanup_policy must be 'retain_reusable' or 'compact'")
    return str(policy)


def static_cleanup_preserve_paths(config_path: str | Path, paths: FinalArtifactPaths) -> set[Path]:
    """Keep configured sources and selected reusable caches inside this work root.

    External inputs are never cleanup candidates. Retention does not certify
    cache validity: owning stages must still verify current content/contracts.
    """
    from ..config.loader import projected_dem_cache_path_for_config
    from ..config.schema import load_yaml

    raw, config_dir = load_yaml(config_path)
    policy = cleanup_policy_from_raw(raw)
    root = paths.output_dir.resolve()
    configured = raw.get("paths", {}) or {}
    preserve: set[Path] = set()

    def keep(path: Path) -> None:
        if path.exists() and path.resolve().is_relative_to(root):
            preserve.add(path.resolve())
            if path.is_file():
                preserve.update(p.resolve() for p in metadata_sidecars_for(path) if p.exists())
                for suffix in (".json", ".source.json", ".metadata.json", ".producer.json"):
                    sidecar = path.with_suffix(path.suffix + suffix)
                    if sidecar.exists():
                        preserve.add(sidecar.resolve())
                if path.suffix.lower() == ".shp":
                    preserve.update(
                        p.resolve()
                        for p in path.parent.glob(path.stem + ".*")
                        if p.is_file() and p.resolve().is_relative_to(root)
                    )

    for key in (
        "regional_dem_path",
        "canopy_height_path",
        "marine_water_mask_path",
        "water_polygon_path",
        "land_polygon_path",
        "land_h3_path",
        "source_cells_path",
        "raw_dem_dir",
    ):
        if configured.get(key):
            keep(resolve_existing_or_relative_path(configured[key], config_dir))
    for dataset in (raw.get("datasets", {}) or {}).values():
        for asset in dataset.get("assets", []) or []:
            if "://" not in str(asset):
                keep(resolve_existing_or_relative_path(asset, config_dir))
    keep(paths.land_static_weights.parent / "viewshed-generation.json")
    if policy == "retain_reusable":
        for path in paths.all_final_paths().values():
            keep(path)
        projected = resolve_path(
            configured.get(
                "projected_dem_path", projected_dem_cache_path_for_config(raw, config_dir)
            ),
            config_dir,
        )
        keep(projected)
        keep(projected.parent / "canonical_domain_rasters")
        for path in (
            root / "land/terrain_surfaces",
            root / "_tmp/distance",
            root / "geometry_cache",
        ):
            keep(path)
        for key in ("partitioned_visibility_dir", "manifest_path"):
            if configured.get(key):
                keep(resolve_path(configured[key], config_dir))
        # Default partition paths are derived independently of output_dir.
        from ..config.paths import versioned_viewshed_name, viewshed_domain_relative

        name = versioned_viewshed_name(raw)
        visibility = resolve_path(
            configured.get(
                "final_visibility_path", Path(viewshed_domain_relative()) / f"{name}.parquet"
            ),
            config_dir,
        )
        keep(visibility.parent / f"{name}_PARTITIONS")
        keep(visibility.parent / f"{name}.csv")
    return preserve


def remove_paths(paths: Iterable[Path]) -> list[Path]:
    removed: list[Path] = []
    for path in paths:
        path = Path(path)
        if not path.exists():
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        removed.append(path)
    return removed


def metadata_sidecars_for(path: Path) -> set[Path]:
    """Return every supported metadata sidecar path for an artifact."""

    return {Path(candidate) for candidate in metadata_sidecar_candidates(path)}


def _known_final_preserve_set(paths: FinalArtifactPaths) -> set[Path]:
    preserve: set[Path] = {
        (paths.land_static_weights.parent / "viewshed-generation.json").resolve()
    }
    for path in paths.all_final_paths().values():
        preserve.add(path.resolve())
        preserve.update(candidate.resolve() for candidate in metadata_sidecars_for(path))
        preserve.add(path.with_suffix(path.suffix + ".producer.json").resolve())
    return preserve


def _remove_empty_dirs(root: Path, *, stop_at: Path) -> list[Path]:
    removed: list[Path] = []
    if not root.exists() or not root.is_dir():
        return removed
    stop_at = stop_at.resolve()
    directories = (candidate for candidate in root.rglob("*") if candidate.is_dir())
    for path in sorted(directories, reverse=True):
        if path.resolve() == stop_at:
            continue
        try:
            path.rmdir()
            removed.append(path)
        except OSError:
            pass
    return removed


def _cleanup_root(path: Path) -> Path:
    root = Path(path).resolve()
    if root == Path(root.anchor) or root == Path.cwd().resolve():
        raise ValueError(f"Refusing to use unsafe cleanup root: {root}")
    return root


def _cleanup_candidate(path: Path, *, root: Path) -> Path:
    candidate = Path(path).resolve()
    if candidate == root or not candidate.is_relative_to(root):
        raise ValueError(
            "Refusing to clean a path outside the configured viewshed output "
            f"directory: candidate={candidate} root={root}"
        )
    return candidate


def _preserve_sets(paths: Iterable[Path], *, root: Path) -> tuple[set[Path], set[Path]]:
    files: set[Path] = set()
    directories: set[Path] = set()
    for value in paths:
        path = Path(value)
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            raise ValueError(
                "Cleanup preserve paths must be inside the configured viewshed "
                f"output directory: preserve={resolved} root={root}"
            )
        if path.exists() and path.is_dir():
            directories.add(resolved)
        elif not path.suffix:
            directories.add(resolved)
        else:
            files.add(resolved)
    return files, directories


def _is_preserved_path(
    path: Path, *, preserved_files: set[Path], preserved_directories: set[Path]
) -> bool:
    resolved = path.resolve()
    return resolved in preserved_files or any(
        resolved == directory or resolved.is_relative_to(directory)
        for directory in preserved_directories
    )


def cleanup_output_tree(
    output_dir: str | Path,
    *,
    preserve_paths: Iterable[Path] = (),
    remove_root_if_empty: bool = False,
) -> list[Path]:
    """Delete files below one safe output root except explicit preserve paths."""

    root = _cleanup_root(Path(output_dir))
    preserved_files, preserved_directories = _preserve_sets(preserve_paths, root=root)
    removed: list[Path] = []
    root.mkdir(parents=True, exist_ok=True)
    files = (candidate for candidate in root.rglob("*") if candidate.is_file())
    for path in sorted(files):
        _cleanup_candidate(path, root=root)
        if _is_preserved_path(
            path,
            preserved_files=preserved_files,
            preserved_directories=preserved_directories,
        ):
            continue
        path.unlink()
        removed.append(path)
    removed.extend(_remove_empty_dirs(root, stop_at=root))
    if remove_root_if_empty and root.exists() and not any(root.iterdir()):
        root.rmdir()
        removed.append(root)
    return removed


def cleanup_data_contract(
    config_path: str | Path,
    *,
    require: Sequence[str] = (),
    source_type: str = "land",
    remove_stage_scratch: bool = False,
    preserve_paths: Iterable[Path] = (),
) -> list[Path]:
    """Remove working artifacts only within the configured output directory."""

    paths = final_artifact_paths(config_path)
    source_type = normalize_source_type(source_type)
    final_paths = paths.as_dict(source_type=source_type)
    for key in require:
        if key not in FINAL_SCHEMAS:
            raise KeyError(f"Unknown final artifact key: {key}")
        validate_parquet_schema(final_paths[key], FINAL_SCHEMAS[key])

    root = _cleanup_root(paths.output_dir)
    preserved = _known_final_preserve_set(paths)
    for path in preserve_paths:
        path = Path(path)
        preserved.add(path.resolve())
        if path.suffix:
            preserved.update(candidate.resolve() for candidate in metadata_sidecars_for(path))

    if not remove_stage_scratch:
        return []

    return cleanup_output_tree(root, preserve_paths=preserved)


__all__ = [
    "cleanup_data_contract",
    "cleanup_output_tree",
    "metadata_sidecars_for",
    "remove_paths",
]
