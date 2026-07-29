from __future__ import annotations
import networkx as nx
import statistics
from core.config import PipelineConfig
import hashlib
import json
import os
from dataclasses import dataclass
from typing import Mapping, Sequence
import math
from pathlib import Path

EdgeId = tuple[int, int, int]
@dataclass(frozen=True)
class EdgeWalkabilityCache:
    schema_version: int
    graph_signature: str
    edge_scores: dict[EdgeId, float]

# In the future, this will be replaced by a call to the Swin transformer returning a score from 1 to 5, but we're not in that phase yet
def compute_walkability() -> float:
    return 5.0


def _load_swin_walkability_model(model_path: str):
    """Load local Swin checkpoint and return a ready-to-run model."""
    import torch
    from transformers.models.swin import SwinConfig, SwinForImageClassification

    checkpoint = torch.load(model_path, map_location="cpu")
    state_dict = checkpoint["model_state_dict"] if "model_state_dict" in checkpoint else checkpoint
    remapped = {}
    for key, value in state_dict.items():
        if key.startswith("backbone."):
            remapped["swin." + key[len("backbone."):]] = value
        else:
            remapped[key] = value

    cfg = SwinConfig(
        image_size=224,
        patch_size=4,
        window_size=7,
        embed_dim=128,
        depths=[2, 2, 18, 2],
        num_heads=[4, 8, 16, 32],
        num_labels=5,
    )
    model = SwinForImageClassification(cfg)
    model.load_state_dict(remapped, strict=False)
    model.eval()
    return model


def predict_walkability_from_image(
    image_path: str = os.path.join("walkability_model", "demo.jpg"),
    model_path: str = os.path.join("walkability_model", "swin-base-patch4-window7-224.pt"),
) -> float:
    """Infer walkability class score (1..5) from a local image."""
    import torch
    from PIL import Image
    from torchvision import transforms

    image = Image.open(image_path).convert("RGB")
    preprocess = transforms.Compose(
        [
            transforms.Resize(256),
            transforms.CenterCrop(224),
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
        ]
    )
    processed = preprocess(image)
    if not isinstance(processed, torch.Tensor):
        raise TypeError("Preprocess pipeline must return a torch.Tensor.")
    pixel_values = processed.unsqueeze(0)
    model = _load_swin_walkability_model(model_path)
    with torch.no_grad():
        logits = model(pixel_values=pixel_values).logits
        pred_idx = int(torch.argmax(logits, dim=-1).item())

    # Convert 0-based class id to walkability score in [1,5].
    return float(pred_idx + 1)


def test_walkability_model_on_demo() -> float:
    """Run one-shot inference on walkability_model/demo.jpg and return predicted score."""
    demo_path = Path("walkability_model") / "demo.jpg"
    model_path = Path("walkability_model") / "swin-base-patch4-window7-224.pt"
    if not demo_path.is_file():
        raise FileNotFoundError(f"Demo image not found: {demo_path}")
    if not model_path.is_file():
        raise FileNotFoundError(f"Model checkpoint not found: {model_path}")
    return predict_walkability_from_image(str(demo_path), model_path=str(model_path))

def get_or_build_edge_walkability_index(cfg: PipelineConfig, G, force_rebuild: bool = False, schema_version: int = 1) -> EdgeWalkabilityCache:
    
    graph_sig = compute_graph_signature(G)
    path = cache_path_for_graph(cfg, graph_sig)

    if not force_rebuild and os.path.exists(path):
        try:
            cached = load_edge_walkability_cache(path)
            if (
                cached.schema_version == schema_version
                and cached.graph_signature == graph_sig
            ):
                return cached
        except (FileNotFoundError, json.JSONDecodeError, ValueError, OSError) as exc:
            print(f"[Walkability cache] Rebuilding cache: {exc}")
    
    edge_scores = build_edge_walkability_index(G)
    save_edge_walkability_cache(cfg, graph_sig, edge_scores, schema_version=schema_version)

    return EdgeWalkabilityCache(
        schema_version=schema_version,
        graph_signature=graph_sig,
        edge_scores=edge_scores,
    )

def compute_graph_signature(G: nx.MultiDiGraph) -> str:
    """Stable content-sensitive hash for this graph snapshot."""
    h = hashlib.sha1()

    # Graph-level metadata
    meta = {
        "is_directed": G.is_directed(),
        "is_multigraph": G.is_multigraph(),
        "crs": str(G.graph.get("crs", "")),
        "node_count": G.number_of_nodes(),
        "edge_count": G.number_of_edges(),
    }
    h.update(json.dumps(meta, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8"))

    # Node content (sorted, rounded for stability)
    node_records: list[str] = []
    for n, data in G.nodes(data=True):
        x = float(data.get("x", 0.0))
        y = float(data.get("y", 0.0))
        node_records.append(f"{int(n)}|{x:.6f}|{y:.6f}")
    for rec in sorted(node_records):
        h.update(rec.encode("utf-8"))

    # Edge content (sorted, includes length + geometry/fallback endpoints)
    edge_records: list[str] = []
    for u, v, k, data in G.edges(keys=True, data=True):
        ux, uy = float(G.nodes[u]["x"]), float(G.nodes[u]["y"])
        vx, vy = float(G.nodes[v]["x"]), float(G.nodes[v]["y"])

        length = float(data.get("length", 0.0))
        geom = data.get("geometry")
        if geom is not None and not geom.is_empty:
            geom_repr = geom.wkt
        else:
            geom_repr = f"LINE({ux:.6f} {uy:.6f},{vx:.6f} {vy:.6f})"

        edge_records.append(
            f"{int(u)}|{int(v)}|{int(k)}|{length:.3f}|{geom_repr}"
        )

    for rec in sorted(edge_records):
        h.update(rec.encode("utf-8"))

    return h.hexdigest()

def cache_path_for_graph(cfg : PipelineConfig, graph_sig: str) -> str:
    """Return full cache file path derived from graph_sig."""
    cache_dir = cfg.walkability_cache_dir
    os.makedirs(cache_dir, exist_ok=True)
    filename = "walkability_edges_" + graph_sig + ".json"
    return os.path.join(cache_dir, filename)

def sample_edge_points(
    G: nx.MultiDiGraph,
    edge_id: tuple[int, int, int],
) -> tuple[tuple[float, float], tuple[float, float], tuple[float, float]]:
    """
    Return (start_point, midpoint, end_point) as (lat, lon) for one edge.
    """
    u, v, k = edge_id
    edge = G.edges[u, v, k]
    x1, y1 = float(G.nodes[u]["x"]), float(G.nodes[u]["y"])
    start = (y1, x1)

    x2, y2 = float(G.nodes[v]["x"]), float(G.nodes[v]["y"])
    end = (y2, x2)

    geom = edge.get("geometry")
    if geom is not None and not geom.is_empty:
        # Shapely LineString uses (x=lon, y=lat)
        mid_pt = geom.interpolate(0.5, normalized=True)
        mid = (float(mid_pt.y), float(mid_pt.x))  # (lat, lon)
    else:
        mid = ((y1 + y2) / 2.0, (x1 + x2) / 2.0)

    return (start, mid, end)


def compute_edge_walkability_score(
    samples: tuple[tuple[float, float], tuple[float, float], tuple[float, float]],
) -> float:
    """
    Run compute_walkability on 3 sampled points and return their mean.
    """
    scores = []
    for point in samples:
        s = compute_walkability()
        scores.append(s)
    return statistics.mean(scores)

def build_edge_walkability_index(
    G: nx.MultiDiGraph,
) -> dict[tuple[int, int,int], float]:
    """
    Compute walkability score for every edge (u, v) -> w_e.
    """
    walkability_scores_for_edges = {}
    for (u,v,key) in G.edges(keys=True):

        samples = sample_edge_points(G,(u,v,key))
        w_e = compute_edge_walkability_score(samples)
        walkability_scores_for_edges[(u,v,key)] = w_e

    return walkability_scores_for_edges

def save_edge_walkability_cache(cfg: PipelineConfig, graph_sig: str, edge_scores: dict[tuple[int, int, int], float], schema_version: int = 1) -> None:
    cache_path = cache_path_for_graph(cfg, graph_sig)
    payload = {
        "schema_version" : schema_version,
        "graph_signature" : graph_sig,
        "edge_scores" : {f"{u}|{v}|{k}" : float(score) for (u, v, k), score in edge_scores.items()}
    }
    tmp = cache_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, sort_keys=True, separators=(",",":"))
    os.replace(tmp, cache_path)

def load_edge_walkability_cache(path: str) -> "EdgeWalkabilityCache":
    """Load and validate cache payload."""
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    if "schema_version" not in payload or "graph_signature" not in payload or "edge_scores" not in payload:
        raise ValueError("Invalid walkability cache payload")
    
    raw_scores = payload["edge_scores"]
    if not isinstance(raw_scores, dict):
        raise ValueError("'edge_scores' must be a dict")

    edge_scores: dict[EdgeId, float] = {}
    for skey, sval in raw_scores.items():
        u, v, k = map(int, skey.split("|"))
        edge_scores[(u, v, k)] = float(sval)

    return EdgeWalkabilityCache(
        schema_version=int(payload["schema_version"]),
        graph_signature=str(payload["graph_signature"]),
        edge_scores=edge_scores,
    )

def compute_path_walkability_from_edges(
    G: nx.MultiDiGraph,
    path_edges: Sequence[tuple[int, int, int]],
    edge_scores: Mapping[tuple[int, int, int], float],
    *,
    length_attr: str = "length",
) -> float:
    
    if not path_edges:
        raise ValueError("Empty path!")
    
    weighted_sum = 0.0
    total_length = 0.0
    for edge_id in path_edges:
        (u,v,k) = edge_id
    
        edge_data = G.edges[u,v,k]
        if length_attr not in edge_data:
            raise KeyError(f"missing {length_attr} on edge {edge_id}")
        l_e = float(edge_data[length_attr])
        if l_e <= 0:
            continue

        if edge_id in edge_scores:
            w_e = float(edge_scores[edge_id])
        else:
            raise KeyError(f"missing walkability score for edge {edge_id}")
        
        weighted_sum += l_e * w_e
        total_length += l_e

    if total_length <= 0:
        raise ValueError("path has zero total length")

    W = weighted_sum / total_length
    return W

def path_nodes_to_path_edges(G, path_nodes):
    path_edges = []
    for u,v in zip(path_nodes[:-1],path_nodes[1:]):
        candidates = G.get_edge_data(u,v)
        if not candidates:
            raise KeyError(f"No edge found for segment ({u}, {v})")

        best_key = min(
            candidates,
            key=lambda k: float(candidates[k].get("length", float("inf")))
        )

        best_len = float(candidates[best_key].get("length", float("inf")))
        if not math.isfinite(best_len):
            raise KeyError(f"All parallel edges missing/invalid 'length' for segment ({u}, {v})")
    
        path_edges.append((u,v,best_key))
    return path_edges


def get_path_walkability_score(cfg, G, path_nodes, force_rebuild=False, schema_version=1) -> float:
    # 1) Ensure edge score cache exists
    cache_obj = get_or_build_edge_walkability_index(
        cfg=cfg, G=G, force_rebuild=force_rebuild,schema_version=schema_version
    )

    # 2) Get the edges corresponding to the nodes
    path_edges = path_nodes_to_path_edges(G=G, path_nodes=path_nodes)

    # 3) Compute path-level walkability from existing edge sequence
    score = compute_path_walkability_from_edges(
        G = G,
        path_edges=path_edges,
        edge_scores=cache_obj.edge_scores,
        length_attr="length",
    )

    return float(score)

if __name__ == '__main__':
    score = predict_walkability_from_image()
    print(f"[Walkability] Predicted score: {score}")



