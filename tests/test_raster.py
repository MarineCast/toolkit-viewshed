from viewshed_toolkit._internal.geo.raster import update_geotiff_profile


def test_small_raster_profile_is_not_tiled() -> None:
    profile = update_geotiff_profile(
        {"width": 8, "height": 8, "dtype": "float32"},
        compress="deflate",
    )

    assert profile["compress"] == "deflate"
    assert profile["predictor"] == 3
    assert "tiled" not in profile
