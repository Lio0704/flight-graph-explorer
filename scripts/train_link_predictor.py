"""Train the route (link) prediction model.

Protocol (no leakage):
1. Read the undirected airport graph from Neo4j.
2. Hide 20% of edges. Features are computed ONLY on the remaining 80% ("observed" graph).
3. Positives = hidden edges. Negatives = the same number of non-edges, half of them
   "hard" (2 hops apart, like real candidates) and half random. Random-only negatives
   would make the task trivially easy and inflate the AUC.
4. Logistic regression, 70/30 stratified split, report ROC-AUC / average precision,
   compared against a common-neighbours-only baseline.
5. Save coefficients + scaler + metrics as JSON (models/link_predictor.json).
"""

from __future__ import annotations

import json
import logging
import random
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler

from app.config import get_settings
from app.database import Neo4jClient
from app.services.link_prediction import FEATURE_NAMES, build_features
from scripts.etl import haversine_km

logger = logging.getLogger("train")
SEED = 42

EDGES_QUERY = """
MATCH (a:Airport)-[:ROUTE]-(b:Airport)
WHERE a.iata < b.iata
RETURN DISTINCT a.iata AS a, b.iata AS b
"""
NODES_QUERY = """
MATCH (a:Airport)
RETURN a.iata AS iata, a.latitude AS lat, a.longitude AS lon,
       [(a)-[:IN_COUNTRY]->(c:Country) | c.name][0] AS country
"""


def split_edges(edges: list[tuple[str, str]], hidden_ratio: float, rng: random.Random):
    """Shuffle and split edges into (observed, hidden)."""
    shuffled = edges[:]
    rng.shuffle(shuffled)
    cut = int(len(shuffled) * hidden_ratio)
    return shuffled[cut:], shuffled[:cut]


def adjacency(edges: list[tuple[str, str]]) -> dict[str, set]:
    """Undirected adjacency sets."""
    adj: dict[str, set] = defaultdict(set)
    for a, b in edges:
        adj[a].add(b)
        adj[b].add(a)
    return adj


def pair_features(pair: tuple[str, str], adj: dict[str, set], nodes: dict[str, dict]) -> list[float]:
    """Compute model features for one pair on the observed graph."""
    a, b = pair
    commons = adj[a] & adj[b]
    adamic = sum(1.0 / np.log(len(adj[m])) for m in commons if len(adj[m]) > 1)
    na, nb = nodes[a], nodes[b]
    distance = haversine_km(na["lat"], na["lon"], nb["lat"], nb["lon"])
    same_country = na["country"] is not None and na["country"] == nb["country"]
    return build_features(len(commons), adamic, (len(adj[a]), len(adj[b])), distance, same_country)


def sample_negatives(count: int, adj: dict[str, set], all_edges: set, rng: random.Random) -> list[tuple[str, str]]:
    """Half 2-hop ('hard') non-edges, half uniformly random non-edges."""
    nodes = sorted(adj)
    negatives: set = set()

    def is_valid(a: str, b: str) -> bool:
        key = (min(a, b), max(a, b))
        return a != b and key not in all_edges and key not in negatives

    attempts = 0
    while len(negatives) < count // 2 and attempts < count * 50:
        attempts += 1
        a = rng.choice(nodes)
        if not adj[a]:
            continue
        b = rng.choice(sorted(adj[rng.choice(sorted(adj[a]))]))
        if is_valid(a, b):
            negatives.add((min(a, b), max(a, b)))
    while len(negatives) < count:
        a, b = rng.choice(nodes), rng.choice(nodes)
        if is_valid(a, b):
            negatives.add((min(a, b), max(a, b)))
    return sorted(negatives)


def build_training_set(edges: list[tuple[str, str]], nodes: dict[str, dict], rng: random.Random):
    """Return X, y built with the hidden-edge protocol."""
    observed, hidden = split_edges(edges, 0.2, rng)
    adj = adjacency(observed)
    positives = [e for e in hidden if adj[e[0]] and adj[e[1]]]
    negatives = sample_negatives(len(positives), adj, set(edges), rng)
    pairs = positives + negatives
    features = np.array([pair_features(p, adj, nodes) for p in pairs])
    labels = np.array([1] * len(positives) + [0] * len(negatives))
    return features, labels


def train(features: np.ndarray, labels: np.ndarray) -> dict:
    """Fit scaler + logistic regression and return the JSON-serialisable model."""
    x_train, x_test, y_train, y_test = train_test_split(
        features, labels, test_size=0.3, random_state=SEED, stratify=labels
    )
    scaler = StandardScaler().fit(x_train)
    model = LogisticRegression(max_iter=1000).fit(scaler.transform(x_train), y_train)
    proba = model.predict_proba(scaler.transform(x_test))[:, 1]
    baseline = x_test[:, FEATURE_NAMES.index("common_neighbours")]
    metrics = {
        "roc_auc": round(float(roc_auc_score(y_test, proba)), 4),
        "average_precision": round(float(average_precision_score(y_test, proba)), 4),
        "accuracy@0.5": round(float(((proba >= 0.5) == y_test).mean()), 4),
        "baseline_common_neighbours_roc_auc": round(float(roc_auc_score(y_test, baseline)), 4),
        "train_size": int(len(y_train)),
        "test_size": int(len(y_test)),
    }
    return {
        "feature_names": FEATURE_NAMES,
        "mean": scaler.mean_.tolist(),
        "scale": scaler.scale_.tolist(),
        "coef": model.coef_[0].tolist(),
        "intercept": float(model.intercept_[0]),
        "metrics": metrics,
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def main() -> None:
    """CLI entry point."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    settings = get_settings()
    client = Neo4jClient.from_settings(settings)
    try:
        edges = [(r["a"], r["b"]) for r in client.read(EDGES_QUERY)]
        nodes = {r["iata"]: r for r in client.read(NODES_QUERY)}
    finally:
        client.close()
    logger.info(f"{len(edges)} undirected edges, {len(nodes)} airports")
    features, labels = build_training_set(edges, nodes, random.Random(SEED))
    payload = train(features, labels)
    path = Path(settings.model_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.info(f"saved {path}: {json.dumps(payload['metrics'])}")
    logger.info(f"coefficients: {json.dumps(dict(zip(FEATURE_NAMES, payload["coef"])), indent=1)}")


if __name__ == "__main__":
    main()
