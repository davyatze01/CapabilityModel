import osmnx as ox
import os
from pathlib import Path
import matplotlib.pyplot as plt

# -----------------------------
# Settings
# -----------------------------

place = "Cagliari, Sardinia, Italy"
network_type = "drive"

years = [2008, 2010, 2012, 2014, 2016, 2018]
output_path = Path("plots") / "past_graphs_cagliari_walk.png"

ox.settings.use_cache = True
ox.settings.log_console = True

# -----------------------------
# Download current graph once
# -----------------------------

print("Downloading current graph...")

ox.settings.overpass_settings = '[out:json][timeout:180]'
G_now = ox.graph_from_place(place, network_type=network_type)

# -----------------------------
# Plot historical graphs
# -----------------------------

fig, axes = plt.subplots(2, 3, figsize=(18, 12))
axes = axes.flatten()

for ax, year in zip(axes, years):
    print(f"Downloading graph for {year}...")

    ox.settings.overpass_settings = (
        f'[out:json][timeout:180][date:"{year}-01-01T00:00:00Z"]'
    )

    try:
        G_old = ox.graph_from_place(place, network_type=network_type)

        # Current graph in light gray
        ox.plot_graph(
            G_now,
            ax=ax,
            node_size=0,
            edge_color="lightgray",
            edge_linewidth=0.5,
            show=False,
            close=False
        )

        # Historical graph in red
        ox.plot_graph(
            G_old,
            ax=ax,
            node_size=0,
            edge_color="red",
            edge_linewidth=0.8,
            show=False,
            close=False
        )

        ax.set_title(f"{year} vs current", fontsize=12)

    except Exception as e:
        ax.set_title(f"{year}: failed", fontsize=12)
        ax.text(
            0.5,
            0.5,
            str(e),
            ha="center",
            va="center",
            wrap=True,
            transform=ax.transAxes
        )

    ax.axis("off")

plt.tight_layout()
output_path.parent.mkdir(parents=True, exist_ok=True)
fig.savefig(output_path, dpi=200, bbox_inches="tight")
print(f"Saved plot to: {output_path.resolve()}")

backend = plt.get_backend().lower()
non_interactive_backends = {"agg", "pdf", "ps", "svg", "template", "cairo"}

if backend in non_interactive_backends:
    print(f"Matplotlib backend '{plt.get_backend()}' is non-interactive; opening saved image instead.")
    if os.name == "nt":
        os.startfile(output_path.resolve())  # type: ignore[attr-defined]
else:
    plt.show(block=True)
