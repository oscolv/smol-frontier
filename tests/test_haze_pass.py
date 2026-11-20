import numpy as np
import pytest
from sklearn.mixture import GaussianMixture
from test_gmm_stage1 import _adaptive_scene

from smol_frontier.classify.gmm import (
    MAT_PENDING,
    Stage1Result,
    Stage2Result,
    haze_pass,
    rescue_water,
    stage1,
    stage2,
    substrate_test,
)
from smol_frontier.config import settings

SHAPE = (3, 12)
KEYS = ("fai", "ndvi", "ndre", "mndwi", "bsi", "nsd", "b08")


class _CutModel:
    def __init__(self, cut: float) -> None:
        self.cut = cut

    def predict_proba(self, x: np.ndarray) -> np.ndarray:
        p0 = (x[:, 0] < self.cut).astype(np.float64)
        return np.column_stack([p0, 1.0 - p0])


ALL_MAT = _CutModel(-np.inf)
ALL_WATER = _CutModel(np.inf)


def _first(model=ALL_MAT, *, thr=None, widening=None, no_mat=False):
    return Stage1Result(
        classes=np.zeros(SHAPE, dtype=np.int16),
        mat_mask=np.zeros(SHAPE, dtype=bool),
        gmm1_sep=3.0,
        nir_thr_used=thr,
        nir_water_frac=0.0,
        no_mat=no_mat,
        confidence=np.full(SHAPE, np.nan, dtype=np.float32),
        n_fai_rescued=0,
        model=model,
        water_comp=0,
        widening=widening,
    )


def _second(model=None):
    return Stage2Result(
        classes=np.zeros(SHAPE, dtype=np.int16),
        gmm2_sep=float("nan"),
        confidence=np.full(SHAPE, np.nan, dtype=np.float32),
        model=model,
        dense_comp=0 if model is not None else -1,
    )


def _pixels(**overrides):
    """A neutral mat-like scene: no soil, no water rule, no rescue, high NSD."""
    base = {
        "fai": 0.08,
        "ndvi": 0.25,
        "ndre": 0.3,
        "mndwi": 0.0,
        "bsi": -0.1,
        "nsd": 0.3,
        "b08": 0.14,
    }
    base.update(overrides)
    return {k: np.full(SHAPE, v, dtype=np.float64) for k, v in base.items()}


def _run(
    idx,
    first,
    second=None,
    *,
    rescue=None,
    base=None,
    valid=None,
    border=None,
):
    rescue = np.ones(SHAPE, dtype=bool) if rescue is None else rescue
    base = np.zeros(SHAPE, dtype=np.int16) if base is None else base
    valid = np.zeros(SHAPE, dtype=bool) if valid is None else valid
    border = np.zeros(SHAPE, dtype=bool) if border is None else border
    return haze_pass(
        rescue,
        base,
        valid,
        border,
        **idx,
        first=first,
        second=second if second is not None else _second(),
    )


def test_output_is_zero_outside_rescue() -> None:
    rescue = np.zeros(SHAPE, dtype=bool)
    rescue[1, 2:5] = True
    out = _run(_pixels(), _first(), rescue=rescue)
    assert (out[~rescue] == 0).all()
    assert (out[rescue] == settings.CLASS_INTERFACE).all()


def test_no_model_or_no_mat_leaves_everything_nodata() -> None:
    assert (_run(_pixels(), _first(model=None)) == 0).all()
    assert (_run(_pixels(), _first(no_mat=True)) == 0).all()


def test_soil_only_in_border_strip() -> None:
    idx = _pixels(bsi=0.2, mndwi=-0.3, ndvi=0.1, ndre=0.1, nsd=0.05, b08=0.2)
    border = np.zeros(SHAPE, dtype=bool)
    border[:, :6] = True
    out = _run(idx, _first(ALL_WATER), border=border)
    assert (out[:, :6] == settings.CLASS_BARE_SOIL).all()
    assert (out[:, 6:] == settings.CLASS_WATER).all()


def test_fixed_nir_water_rule_skips_the_gmm() -> None:
    idx = _pixels(b08=0.10, nsd=0.05, fai=0.08)
    out = _run(idx, _first(ALL_MAT))
    assert (out == settings.CLASS_WATER).all()
    out = _run(_pixels(b08=0.10, nsd=0.3, fai=0.08), _first(ALL_MAT))
    assert (out == settings.CLASS_INTERFACE).all()


def test_prescreen_widening_keeps_the_nsd_guard() -> None:
    first = _first(ALL_MAT, thr=0.2, widening="prescreen")
    low_nsd = _pixels(b08=0.15, fai=0.01, nsd=0.10)
    high_nsd = _pixels(b08=0.15, fai=0.01, nsd=0.30)
    assert (_run(low_nsd, first) == settings.CLASS_WATER).all()
    assert (_run(high_nsd, first) == settings.CLASS_INTERFACE).all()


def test_adaptive_widening_has_no_nsd_guard() -> None:
    first = _first(ALL_MAT, thr=0.2, widening="adaptive")
    high_nsd = _pixels(b08=0.15, fai=0.01, nsd=0.30)
    assert (_run(high_nsd, first) == settings.CLASS_WATER).all()
    high_fai = _pixels(b08=0.15, fai=0.06, nsd=0.30)
    assert (_run(high_fai, first) == settings.CLASS_INTERFACE).all()


def test_no_widening_means_no_widened_water() -> None:
    idx = _pixels(b08=0.15, fai=0.01, nsd=0.10)
    assert (_run(idx, _first(ALL_MAT)) == settings.CLASS_INTERFACE).all()


@pytest.mark.parametrize("widening", ["prescreen", "adaptive"])
def test_fai_rescue_of_the_ambiguous_nir_band(widening) -> None:
    idx = _pixels(b08=0.15, fai=0.06, nsd=0.30)
    first = _first(ALL_WATER, thr=0.2, widening=widening)
    assert (_run(idx, first) == settings.CLASS_INTERFACE).all()
    above = _pixels(b08=0.25, fai=0.06, nsd=0.30, ndvi=0.1)
    assert (_run(above, first) == settings.CLASS_WATER).all()


def test_no_fai_rescue_without_widening() -> None:
    idx = _pixels(b08=0.15, fai=0.06, nsd=0.30)
    assert (_run(idx, _first(ALL_WATER)) == settings.CLASS_WATER).all()


def test_stage1_model_decides_with_tau_water() -> None:
    idx = _pixels(fai=0.08)
    idx["fai"][:, :6] = 0.01
    out = _run(idx, _first(_CutModel(0.03)))
    assert (out[:, :6] == settings.CLASS_WATER).all()
    assert (out[:, 6:] == settings.CLASS_INTERFACE).all()


def test_stage2_model_splits_dense_and_interface() -> None:
    idx = _pixels()
    idx["ndvi"][:, 6:] = 0.7
    # Component 1 gets P=1 when NDVI >= 0.5 (not below the cut); dense_comp=1
    #   makes it the dense component.
    second = _second(model=_CutModel(0.5))
    second.dense_comp = 1
    out = _run(idx, _first(ALL_MAT), second)
    assert (out[:, :6] == settings.CLASS_INTERFACE).all()
    assert (out[:, 6:] == settings.CLASS_DENSE_HYACINTH).all()


def test_water_to_interface_rescue() -> None:
    idx = _pixels(ndvi=0.4, b08=0.2, fai=0.01)
    assert (_run(idx, _first(ALL_WATER)) == settings.CLASS_INTERFACE).all()


def test_substrate_anchor_comes_from_the_base_map_only() -> None:
    idx = _pixels()
    idx["nsd"][:] = 0.3
    idx["nsd"][:, 9:] = 0.05
    rescue = np.zeros(SHAPE, dtype=bool)
    rescue[:, 8:] = True
    base = np.zeros(SHAPE, dtype=np.int16)
    valid = np.zeros(SHAPE, dtype=bool)
    base[:, 0] = settings.CLASS_BARE_SOIL
    valid[:, :8] = True
    idx["ndvi"][:, 0] = 0.1
    # Column 8 is rescued soil (border strip) — must NOT anchor anything.
    border = np.zeros(SHAPE, dtype=bool)
    border[:, 8] = True
    idx["bsi"][:, 8] = 0.2
    idx["mndwi"][:, 8] = -0.3
    idx["ndvi"][:, 8] = 0.1
    idx["ndre"][:, 8] = 0.1
    idx["nsd"][:, 8] = 0.05
    out = _run(
        idx, _first(ALL_MAT), rescue=rescue, base=base, valid=valid, border=border
    )
    assert (out[:, 8] == settings.CLASS_BARE_SOIL).all()
    # Columns 9+ are 9+ px from the base anchor (> 5 iterations): stay mat.
    assert (out[:, 9:] == settings.CLASS_INTERFACE).all()

    # Move the base anchor to column 7: candidates within 5 px become soil.
    base[:] = 0
    base[:, 7] = settings.CLASS_BARE_SOIL
    idx["ndvi"][:, 7] = 0.1
    out = _run(
        idx, _first(ALL_MAT), rescue=rescue, base=base, valid=valid, border=border
    )
    assert (out[:, 9:13] == settings.CLASS_BARE_SOIL).all()
    assert (out[~rescue] == 0).all()


def test_substrate_anchor_needs_valid_low_ndvi_base_soil() -> None:
    idx = _pixels(nsd=0.05)
    rescue = np.zeros(SHAPE, dtype=bool)
    rescue[:, 6:] = True
    base = np.zeros(SHAPE, dtype=np.int16)
    base[:, 5] = settings.CLASS_BARE_SOIL
    idx["ndvi"][:, 5] = 0.1
    out = _run(idx, _first(ALL_MAT), rescue=rescue, base=base)
    assert (out[rescue] == settings.CLASS_INTERFACE).all()


def _bimodal(rng, shape=(80, 50), split=50):
    n_w, n_m = split * shape[1], (shape[0] - split) * shape[1]
    water = {
        "fai": rng.normal(0.0, 0.008, n_w),
        "ndvi": rng.normal(0.02, 0.05, n_w),
        "ndre": rng.normal(0.0, 0.05, n_w),
        "mndwi": rng.normal(0.3, 0.1, n_w),
        "bsi": rng.normal(-0.05, 0.03, n_w),
        "nsd": rng.normal(0.2, 0.03, n_w),
        "b08": rng.normal(0.095, 0.015, n_w),
    }
    mat = {
        "fai": rng.normal(0.09, 0.015, n_m),
        "ndvi": np.concatenate(
            [rng.normal(0.72, 0.05, n_m // 2), rng.normal(0.38, 0.05, n_m - n_m // 2)]
        ),
        "ndre": np.concatenate(
            [rng.normal(0.55, 0.05, n_m // 2), rng.normal(0.22, 0.05, n_m - n_m // 2)]
        ),
        "mndwi": rng.normal(-0.3, 0.1, n_m),
        "bsi": rng.normal(-0.1, 0.05, n_m),
        "nsd": rng.normal(0.2, 0.05, n_m),
        "b08": rng.normal(0.28, 0.03, n_m),
    }
    return {k: np.concatenate([water[k], mat[k]]).reshape(shape) for k in KEYS}


def test_stage_results_carry_the_fitted_models() -> None:
    scene = _bimodal(np.random.default_rng(7))
    shape = scene["fai"].shape
    valid = np.ones(shape, dtype=bool)
    border = np.zeros(shape, dtype=bool)
    first = stage1(*[scene[k] for k in KEYS], valid, border)
    assert isinstance(first.model, GaussianMixture)
    assert first.water_comp == int(np.argmin(first.model.means_[:, 0]))
    assert first.widening is None
    second = stage2(scene["ndvi"], scene["ndre"], first.classes, first.confidence)
    assert isinstance(second.model, GaussianMixture)
    assert second.dense_comp == int(np.argmax(second.model.means_[:, 0]))


def test_haze_pass_reproduces_the_first_pass_on_identical_pixels() -> None:
    scene = _bimodal(np.random.default_rng(7))
    shape = scene["fai"].shape
    valid = np.ones(shape, dtype=bool)
    border = np.zeros(shape, dtype=bool)
    first = stage1(*[scene[k] for k in KEYS], valid, border)
    second = stage2(scene["ndvi"], scene["ndre"], first.classes, first.confidence)
    r = rescue_water(
        second.classes, second.confidence, valid, scene["ndvi"], scene["b08"]
    )
    base = substrate_test(r.classes, r.confidence, valid, scene["nsd"], scene["ndvi"])
    out = haze_pass(
        np.ones(shape, dtype=bool),
        np.zeros(shape, dtype=np.int16),
        np.zeros(shape, dtype=bool),
        border,
        **scene,
        first=first,
        second=second,
    )
    assert np.array_equal(out, base.classes)
    assert MAT_PENDING not in np.unique(out)


def test_stage1_records_prescreen_widening() -> None:
    rng = np.random.default_rng(11)
    shape = (80, 50)
    n_w, n_i, n_m = 40 * 50, 15 * 50, 25 * 50
    parts = [
        {
            "fai": rng.normal(0.0, 0.008, n_w),
            "ndvi": rng.normal(0.02, 0.05, n_w),
            "ndre": rng.normal(0.0, 0.05, n_w),
            "mndwi": rng.normal(0.3, 0.1, n_w),
            "bsi": rng.normal(-0.05, 0.03, n_w),
            "nsd": rng.normal(0.05, 0.02, n_w),
            "b08": rng.normal(0.16, 0.015, n_w),
        },
        {
            "fai": rng.normal(0.03, 0.015, n_i),
            "ndvi": rng.normal(0.25, 0.1, n_i),
            "ndre": rng.normal(0.2, 0.1, n_i),
            "mndwi": rng.normal(0.0, 0.1, n_i),
            "bsi": rng.normal(-0.05, 0.03, n_i),
            "nsd": rng.normal(0.2, 0.05, n_i),
            "b08": rng.normal(0.20, 0.03, n_i),
        },
        {
            "fai": rng.normal(0.09, 0.015, n_m),
            "ndvi": rng.normal(0.6, 0.08, n_m),
            "ndre": rng.normal(0.4, 0.1, n_m),
            "mndwi": rng.normal(-0.3, 0.1, n_m),
            "bsi": rng.normal(-0.1, 0.05, n_m),
            "nsd": rng.normal(0.2, 0.05, n_m),
            "b08": rng.normal(0.30, 0.03, n_m),
        },
    ]
    scene = {k: np.concatenate([p[k] for p in parts]).reshape(shape) for k in KEYS}
    first = stage1(
        *[scene[k] for k in KEYS],
        np.ones(shape, dtype=bool),
        np.zeros(shape, dtype=bool),
    )
    assert first.nir_thr_used is not None
    assert first.widening == "prescreen"


def test_stage1_records_adaptive_widening() -> None:
    scene, shape = _adaptive_scene()
    first = stage1(
        *[scene[k] for k in KEYS],
        np.ones(shape, dtype=bool),
        np.zeros(shape, dtype=bool),
    )
    assert first.nir_thr_used is not None
    assert first.widening == "adaptive"


def test_no_mat_stage1_has_no_model() -> None:
    rng = np.random.default_rng(13)
    shape = (40, 40)
    n = shape[0] * shape[1]
    scene = {
        "fai": rng.normal(0.0, 0.008, n),
        "ndvi": rng.normal(0.02, 0.05, n),
        "ndre": rng.normal(0.0, 0.05, n),
        "mndwi": rng.normal(0.3, 0.1, n),
        "bsi": rng.normal(-0.05, 0.03, n),
        "nsd": rng.normal(0.05, 0.02, n),
        "b08": rng.normal(0.09, 0.02, n),
    }
    scene = {k: v.reshape(shape) for k, v in scene.items()}
    first = stage1(
        *[scene[k] for k in KEYS],
        np.ones(shape, dtype=bool),
        np.zeros(shape, dtype=bool),
    )
    assert first.no_mat is True
    assert first.model is None
