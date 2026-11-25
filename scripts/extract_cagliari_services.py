"""Export all scored Cagliari cells as capability, criteria, quartiere rows.

Run: .venv/bin/python scripts/extract_cagliari_services.py
Criteria follow the service order in config/capability.csv, rounded to three
 decimal places. Quartieri are approximate nearest labels, without a cutoff.
"""
import argparse
import ast
import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path,
                        default=ROOT / 'outputs/export/Cagliari_service_alternatives')
    args = parser.parse_args()
    scores_path = ROOT / 'outputs/gpkg/Cagliari/Cagliari_hex.csv'
    scores = pd.read_csv(scores_path, dtype={'hex_id': str})
    config = pd.read_csv(ROOT / 'config/capability.csv').set_index('capability')
    names = {'restorativeness': 'restorativeness', 'nutrition': 'nutrition', 'care': 'healthcare'}
    criteria_order = {target: ast.literal_eval(config.loc[source, 'services'])
                      for source, target in names.items()}
    columns = [f'grid_mean_service_{s}' for services in criteria_order.values() for s in services]
    if not scores.hex_id.is_unique:
        raise ValueError('Duplicate source cell IDs')
    complete = scores[columns].notna().all(axis=1)
    partial = scores[columns].notna().any(axis=1) & ~complete
    if partial.any():
        raise ValueError('Some cells have incomplete service scores')
    excluded = int((~complete).sum())
    scores = scores.loc[complete].copy()
    grid = gpd.read_file(scores_path.with_name('Cagliari.gpkg'), layer='zz_capability_grid')
    if not grid.hex_id.is_unique:
        raise ValueError('Duplicate geometry cell IDs')
    grid = grid.set_index('hex_id').loc[scores.hex_id].reset_index()
    if not np.allclose(scores[columns].to_numpy(dtype=float), grid[columns].to_numpy(dtype=float),
                       rtol=1e-12, atol=1e-12):
        raise ValueError('CSV and GeoPackage service scores differ')
    centers = grid.to_crs(32632).geometry.centroid
    boundary = gpd.read_file(ROOT / 'shapefile_base/Cagliari Shapefile.shp')
    boundary = boundary.to_crs(32632).geometry.unary_union
    labels = gpd.read_file(ROOT / 'outputs/poi_exports/Cagliari/place_labels.gpkg').to_crs(32632)
    labels = labels[(labels.place_kind == 'quartiere') & labels.geometry.apply(boundary.covers)]
    labels = labels.sort_values('name').reset_index(drop=True)
    if labels.empty:
        raise ValueError('No quartiere labels inside Cagliari')
    distances = np.column_stack([centers.distance(point).to_numpy() for point in labels.geometry])
    nearest_labels = labels.name.to_numpy()[distances.argmin(axis=1)]
    rows = []
    for capability, services in criteria_order.items():
        values = scores[[f'grid_mean_service_{s}' for s in services]].to_numpy(dtype=float)
        for criteria, quartiere in zip(values, nearest_labels):
            array = '[' + ', '.join(f'{v:.3f}' for v in criteria) + ']'
            rows.append({'capability': capability, 'criteria': array, 'quartiere': quartiere})
    args.output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = args.output_dir / 'service_alternatives.csv'
    pd.DataFrame(rows, columns=['capability', 'criteria', 'quartiere']).to_csv(
        csv_path, index=False, encoding='utf-8-sig')
    metadata = {
        'scored_cells': len(scores), 'rows': len(rows), 'empty_cells_excluded': excluded,
        'criteria_order': criteria_order,
        'row_order': 'Capability order listed above, then original scored cell order in Cagliari_hex.csv.',
        'rounding': 'Nearest three decimal places, serialized with three decimal digits.',
        'quartiere': 'Previous nearest_quartiere_label: nearest cached OSM quartiere label inside Cagliari. '
                     'No distance cutoff or municipal membership filter on cells. Approximate, not polygon membership.',
        'source': str(scores_path.relative_to(ROOT)),
    }
    (args.output_dir / 'metadata.json').write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print(json.dumps({'csv': str(csv_path), **metadata}, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
