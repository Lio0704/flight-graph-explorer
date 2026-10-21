"""Route (link) prediction: shared feature definition + model serving.

The same `build_features` function is used at training time
(`scripts/train_link_predictor.py`, features computed in Python on a graph with
hidden edges) and at serving time (features computed in Cypher by
`ROUTE_CANDIDATES`). Keeping a single definition avoids train/serve skew.

The model is a logistic regression stored as plain JSON (coefficients,
scaler mean/scale, metrics): readable, versionable, and no pickle loading.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from app.exceptions import ModelNotTrainedError

FEATURE_NAMES = [
    "common_neighbours",
    "adamic_adar",
    "jaccard",
    "log_preferential_attachment",
    "log_distance_km",
    "same_country",
]


def build_features(
    common_neighbours: int, adamic_adar: float, degrees: tuple[int, int], distance_km: float, same_country: bool
) -> list[float]:
    """Topological + geographic features for a candidate airport pair.

    * common neighbours / Adamic-Adar / Jaccard: triadic closure, i.e. two airports
      already linked to the same hubs tend to get a direct route.
    * preferential attachment (deg_a * deg_b, log-scaled): big airports connect to big airports.
    * distance: most new routes are short/medium haul.
    * same country: domestic routes are cheaper to open.
    """
    deg_a, deg_b = degrees
    union = deg_a + deg_b - common_neighbours
    jaccard = common_neighbours / union if union > 0 else 0.0
    return [
        float(common_neighbours),
        float(adamic_adar),
        jaccard,
        math.log1p(deg_a * deg_b),
        math.log1p(max(distance_km, 0.0)),
        1.0 if same_country else 0.0,
    ]


class LinkPredictor:
    """Logistic-regression scorer loaded from JSON."""

    def __init__(self, payload: dict) -> None:
        if payload.get("feature_names") != FEATURE_NAMES:
            raise ModelNotTrainedError("Model features do not match the code: retrain with `make train`.")
        self._mean = payload["mean"]
        self._scale = payload["scale"]
        self._coef = payload["coef"]
        self._intercept = payload["intercept"]
        self.metrics = payload.get("metrics", {})
        self.version = payload.get("trained_at", "unknown")

    @classmethod
    def load(cls, path: str | Path) -> "LinkPredictor":
        """Load a model file, raising ModelNotTrainedError when absent."""
        path = Path(path)
        if not path.exists():
            raise ModelNotTrainedError(f"No model at {path}. Run `make train` (or the docker seeder).")
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def predict_proba(self, features: list[float]) -> float:
        """Probability that the route exists / will exist."""
        z = self._intercept
        for value, mean, scale, coef in zip(features, self._mean, self._scale, self._coef):
            z += coef * (value - mean) / (scale or 1.0)
        return 1.0 / (1.0 + math.exp(-z))
