"""Tests for features, model serving and the training protocol."""

import json
import random

import pytest

from app.exceptions import ModelNotTrainedError
from app.services.link_prediction import FEATURE_NAMES, LinkPredictor, build_features
from scripts.train_link_predictor import adjacency, build_training_set, sample_negatives, split_edges, train


def test_build_features():
    features = build_features(4, 2.5, (10, 20), 1000.0, True)
    assert len(features) == len(FEATURE_NAMES)
    assert features[2] == pytest.approx(4 / 26)
    assert features[5] == 1.0


def test_missing_model(tmp_path):
    with pytest.raises(ModelNotTrainedError):
        LinkPredictor.load(tmp_path / "missing.json")


def test_feature_mismatch_rejected():
    with pytest.raises(ModelNotTrainedError):
        LinkPredictor({"feature_names": ["other"]})


def test_predict_proba(trained_model):
    model = LinkPredictor.load(trained_model)
    close = model.predict_proba(build_features(20, 6.0, (50, 60), 300.0, True))
    far = model.predict_proba(build_features(0, 0.0, (2, 3), 15000.0, False))
    assert 0 < far < close < 1


def toy_graph(seed=1, size=60):
    """Clustered random graph: dense inside 3 groups, few links between them."""
    rng = random.Random(seed)
    nodes = {f"N{i:02d}": {"lat": (i % 3) * 10.0, "lon": (i % 3) * 10.0, "country": f"C{i % 3}"}
             for i in range(size)}
    names = sorted(nodes)
    edges = set()
    for a in names:
        for b in names:
            same = nodes[a]["country"] == nodes[b]["country"]
            if a < b and rng.random() < (0.35 if same else 0.01):
                edges.add((a, b))
    return sorted(edges), nodes


def test_split_and_negatives_do_not_leak():
    edges, _ = toy_graph()
    rng = random.Random(0)
    observed, hidden = split_edges(edges, 0.2, rng)
    assert not set(observed) & set(hidden)
    negatives = sample_negatives(20, adjacency(observed), set(edges), rng)
    assert len(negatives) == 20 and not set(negatives) & set(edges)


def test_training_beats_chance():
    edges, nodes = toy_graph()
    features, labels = build_training_set(edges, nodes, random.Random(42))
    payload = train(features, labels)
    assert payload["feature_names"] == FEATURE_NAMES
    assert payload["metrics"]["roc_auc"] > 0.7
    json.dumps(payload)
