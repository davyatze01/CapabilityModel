from __future__ import annotations

from pathlib import Path
import os

import math

import matplotlib as mpl

# On some Windows Python builds, Agg PNG rendering can crash with illegal
# instruction errors in native extensions. Prefer SVG backend/output there.
if os.name == "nt":
    mpl.use("svg")

import matplotlib.pyplot as plt
from matplotlib.collections import LineCollection
import networkx as nx
import numpy as np
import pandas as pd


BACKGROUND_COLOR = "#343434"
TEXT_COLOR = "#E8E6E3"
COLORMAP = "inferno"
DEFAULT_GRAPH_MODE = "bike"
DEFAULT_K_METERS = 50.0


def pick_graphml_file(graph_dir: Path, preferred_mode: str = DEFAULT_GRAPH_MODE) -> Path:
    """Select the GraphML file to use as the base network.

    The script looks inside `graph/` and prefers a mode-specific file such as
    `*_bike.graphml`. If no mode-specific file exists, it falls back to the
    first GraphML file found in that directory.
    """
    graphml_files = sorted(graph_dir.glob("*.graphml"))
    if not graphml_files:
        raise FileNotFoundError(f"No .graphml files found in {graph_dir}")

    preferred_matches = sorted(graph_dir.glob(f"*_{preferred_mode}.graphml"))
    if preferred_matches:
        return preferred_matches[0]

    return graphml_files[0]


def load_graph(graph_path: Path) -> nx.Graph:
    """Load the graph from GraphML and normalize node coordinates to floats.

    GraphML attributes are typically read as strings, so `x` and `y` are cast
    to numeric values here to make later geometric calculations safe.
    """
    graph = nx.read_graphml(graph_path)
    for node_id, attrs in graph.nodes(data=True):
        attrs["x"] = float(attrs["x"])
        attrs["y"] = float(attrs["y"])
    return graph


def find_value_column(frame: pd.DataFrame) -> str:
    """Determine which CSV column contains the scalar values to visualize.

    Preferred convention:
    - a capability column such as `capability_care`

    Fallback convention:
    - exactly one non-coordinate data column other than `node_id`, `lat`, `lon`
    """
    capability_columns = [column for column in frame.columns if column.startswith("capability_")]
    if capability_columns:
        return capability_columns[0]

    fallback_columns = [column for column in frame.columns if column not in {"node_id", "lat", "lon"}]
    if len(fallback_columns) == 1:
        return fallback_columns[0]

    raise ValueError(
        "Could not determine the value column. Expected a 'capability_*' column "
        "or exactly one non-coordinate data column."
    )


def load_experiment_values(csv_path: Path) -> tuple[pd.DataFrame, str]:
    """Read one experiment CSV and keep only node IDs plus the value column.

    Assumptions about the CSV:
    - it contains a `node_id` column
    - it contains at least one scalar value column that can be plotted

    The value column is converted to numeric and invalid rows are dropped so
    downstream plotting only sees usable data.
    """
    frame = pd.read_csv(csv_path)
    if "node_id" not in frame.columns:
        raise ValueError(f"{csv_path.name} must contain a 'node_id' column.")

    value_column = find_value_column(frame)
    frame = frame[["node_id", value_column]].copy()
    frame["node_id"] = frame["node_id"].astype(str)
    frame[value_column] = pd.to_numeric(frame[value_column], errors="coerce")
    frame = frame.dropna(subset=[value_column])
    return frame, value_column


def assign_node_values(graph: nx.Graph, node_values: pd.DataFrame, value_column: str) -> dict[str, float]:
    """Attach experiment values to graph nodes and return a lookup dictionary.

    The returned dictionary is used during edge processing, while the node
    attributes make the graph self-describing for debugging or future reuse.
    """
    value_map = dict(zip(node_values["node_id"], node_values[value_column]))
    nx.set_node_attributes(graph, {node_id: value_map.get(node_id) for node_id in graph.nodes}, value_column)
    return value_map


def haversine_meters(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Compute great-circle distance in meters between two lon/lat points."""
    radius = 6_371_000.0
    lon1_rad = math.radians(lon1)
    lat1_rad = math.radians(lat1)
    lon2_rad = math.radians(lon2)
    lat2_rad = math.radians(lat2)
    dlon = lon2_rad - lon1_rad
    dlat = lat2_rad - lat1_rad
    a = math.sin(dlat / 2.0) ** 2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon / 2.0) ** 2
    return 2.0 * radius * math.asin(math.sqrt(a))


def compute_edge_length(edge_data: dict, x1: float, y1: float, x2: float, y2: float) -> float:
    """Return the best available edge length in meters.

    Priority:
    1. use the `length` attribute already stored on the edge in GraphML
    2. otherwise estimate the length from endpoint coordinates
    """
    raw_length = edge_data.get("length")
    if raw_length is not None:
        try:
            return float(raw_length)
        except (TypeError, ValueError):
            pass
    return haversine_meters(x1, y1, x2, y2)


def interpolate_edge_segments(
    x1: float,
    y1: float,
    value1: float,
    x2: float,
    y2: float,
    value2: float,
    edge_length_m: float,
    k_meters: float,
) -> tuple[list[list[tuple[float, float]]], list[float]]:
    """Split one edge into smaller segments and interpolate values along it.

    If an edge has endpoints with values `value1` and `value2`, this function
    samples the edge at distances:
    `0, k, 2k, ..., L`

    For each consecutive pair of sampled points, one short line segment is
    created. Its color-driving value is the interpolated value at the starting
    point of that small segment.
    """
    if edge_length_m <= 0:
        return [], []

    # Build the distance sequence requested by the user specification.
    # The final edge length is appended explicitly so the last short segment
    # always reaches the target node even when L is not a multiple of k.
    distances = np.arange(0.0, edge_length_m, k_meters)
    if distances.size == 0 or distances[0] != 0.0:
        distances = np.insert(distances, 0, 0.0)
    if distances[-1] < edge_length_m:
        distances = np.append(distances, edge_length_m)

    # Convert distances to normalized interpolation coordinates in [0, 1].
    # Those normalized values are then used both for geometry interpolation
    # and scalar interpolation.
    t_values = distances / edge_length_m
    xs = x1 + (x2 - x1) * t_values
    ys = y1 + (y2 - y1) * t_values
    values = value1 + (value2 - value1) * t_values

    segments: list[list[tuple[float, float]]] = []
    segment_values: list[float] = []
    for index in range(len(xs) - 1):
        # Each item in `segments` is a tiny line from sample point i to i+1.
        # The matching item in `segment_values` stores the interpolated value
        # at the start of that tiny segment, which is the rule requested.
        start = (float(xs[index]), float(ys[index]))
        end = (float(xs[index + 1]), float(ys[index + 1]))
        segments.append([start, end])
        segment_values.append(float(values[index]))

    return segments, segment_values


def build_colored_segments(
    graph: nx.Graph,
    value_map: dict[str, float],
    value_column: str,
    k_meters: float,
) -> tuple[list[list[tuple[float, float]]], list[float]]:
    """Process the full graph and collect every colored mini-segment to plot."""
    segments: list[list[tuple[float, float]]] = []
    segment_values: list[float] = []

    # OSM-style graphs are often MultiGraphs, so the edge iterator has two
    # possible shapes. This branch keeps the rest of the code uniform.
    edge_iter = graph.edges(keys=True, data=True) if graph.is_multigraph() else graph.edges(data=True) # type: ignore

    for edge in edge_iter:
        if graph.is_multigraph():
            u, v, _, edge_data = edge # type: ignore
        else:
            u, v, edge_data = edge

        # An edge can only be colored if both of its endpoint nodes have values
        # in the experiment CSV. If one endpoint is missing, the edge is skipped.
        value_u = value_map.get(str(u))
        value_v = value_map.get(str(v))
        if value_u is None or value_v is None:
            continue

        node_u = graph.nodes[u]
        node_v = graph.nodes[v]
        x1, y1 = float(node_u["x"]), float(node_u["y"])
        x2, y2 = float(node_v["x"]), float(node_v["y"])
        edge_length_m = compute_edge_length(edge_data, x1, y1, x2, y2)

        # Generate all sub-segments for the current edge and append them to the
        # global plotting buffers.
        edge_segments, edge_segment_values = interpolate_edge_segments(
            x1=x1,
            y1=y1,
            value1=float(value_u),
            x2=x2,
            y2=y2,
            value2=float(value_v),
            edge_length_m=edge_length_m,
            k_meters=k_meters,
        )
        segments.extend(edge_segments)
        segment_values.extend(edge_segment_values)

    if not segments:
        raise ValueError(f"No plottable segments found for value column '{value_column}'.")

    return segments, segment_values


def plot_experiment(
    graph: nx.Graph,
    csv_path: Path,
    output_dir: Path,
    k_meters: float = DEFAULT_K_METERS,
    dpi: int = 250,
) -> Path:
    """Create and save the plot corresponding to a single experiment CSV."""
    node_values, value_column = load_experiment_values(csv_path)
    value_map = assign_node_values(graph, node_values, value_column)
    segments, segment_values = build_colored_segments(graph, value_map, value_column, k_meters)

    # The color normalization ensures the full colormap spans the value range
    # found in the current experiment.
    values_array = np.asarray(segment_values, dtype=float)
    norm = mpl.colors.Normalize(vmin=float(values_array.min()), vmax=float(values_array.max())) # type: ignore
    line_collection = LineCollection(
        segments,
        cmap=plt.get_cmap(COLORMAP),
        norm=norm,
        linewidths=1.0,
        alpha=0.95,
    )
    line_collection.set_array(values_array)

    # Create a dark-themed figure similar to the visual style requested earlier.
    fig, ax = plt.subplots(figsize=(16, 9), facecolor=BACKGROUND_COLOR)
    ax.set_facecolor(BACKGROUND_COLOR)
    ax.add_collection(line_collection)
    ax.autoscale()
    ax.set_aspect("equal")
    ax.set_axis_off()
    ax.set_title(
        f"{value_column}, interpolation step {int(k_meters)}m",
        color=TEXT_COLOR,
        fontsize=16,
        pad=18,
    )

    # The colorbar explains how line colors map back to interpolated values.
    colorbar = fig.colorbar(line_collection, ax=ax, fraction=0.03, pad=0.02)
    colorbar.ax.tick_params(colors=TEXT_COLOR, labelsize=10)
    colorbar.outline.set_edgecolor(TEXT_COLOR) # type: ignore
    colorbar.set_label("Interpolated node value", color=TEXT_COLOR, fontsize=12)

    # Save one image per CSV using a filename derived from the experiment name.
    output_dir.mkdir(parents=True, exist_ok=True)
    output_format = "svg" if os.name == "nt" else "png"
    output_path = output_dir / f"{csv_path.stem}_edge_interp_{k_meters}.{output_format}"
    fig.tight_layout()
    fig.savefig(
        output_path,
        dpi=dpi,
        facecolor=fig.get_facecolor(),
        bbox_inches="tight",
        format=output_format,
    )
    plt.close(fig)
    return output_path


def plot_all_experiments(
    graph_dir: str | Path = "graph",
    experiments_dir: str | Path = "experiments",
    output_dir: str | Path = "plots/edge_interpolation",
    graph_path: str | Path | None = None,
    k_meters: float = DEFAULT_K_METERS,
) -> list[Path]:
    """Run the full workflow for every experiment CSV in the folder."""
    graph_dir = Path(graph_dir)
    experiments_dir = Path(experiments_dir)
    output_dir = Path(output_dir)

    # Load the graph once and reuse it for every CSV to avoid repeated I/O.
    selected_graph = Path(graph_path) if graph_path is not None else pick_graphml_file(graph_dir)
    graph = load_graph(selected_graph)

    created_plots: list[Path] = []
    for csv_path in sorted(experiments_dir.glob("*.csv")):
        try:
            plot_path = plot_experiment(graph, csv_path, output_dir, k_meters=k_meters)
        except ValueError:
            # Files that do not match the expected node/value structure are
            # skipped instead of stopping the full batch process.
            continue
        created_plots.append(plot_path)

    return created_plots


if __name__ == "__main__":
    # Script entry point so the file can be launched directly with Python.
    generated_plots = plot_all_experiments()
    for plot_path in generated_plots:
        print(plot_path)
