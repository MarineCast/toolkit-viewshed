"""Distance decay weights for viewshed source-target pairs."""

from ...config.distance import (
    DistanceRuntime,
    DistanceWeightConfig,
    load_distance_runtime,
    load_distance_weight_config,
    resolved_max_distance_km,
)
from .cli import main
from .compute import distance_weight_values
from .products import (
    DistanceProfile,
    build_distance_profile,
    build_pair_distances,
    validate_distance_product,
)

__all__ = [
    "DistanceProfile",
    "DistanceRuntime",
    "DistanceWeightConfig",
    "build_distance_profile",
    "build_pair_distances",
    "distance_weight_values",
    "load_distance_runtime",
    "load_distance_weight_config",
    "main",
    "resolved_max_distance_km",
    "validate_distance_product",
]
