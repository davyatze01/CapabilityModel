"""Utility to modify QGIS project to zoom to specific layer on open."""
import zipfile
import tempfile
import shutil
from pathlib import Path
import xml.etree.ElementTree as ET


def zoom_to_layer(qgz_path: str, target_layer_name: str | None = None) -> None:
    """Modify QGIS project to zoom to target layer on open.
    
    Inputs:
    - qgz_path: path to the .qgz file
    - target_layer_name: name of the layer to zoom to (if None, tries "Capability model output")
    
    Outputs:
    - None. Modifies the .qgz file in place.
    """
    import sqlite3
    
    qgz_path = Path(qgz_path)
    if not qgz_path.exists():
        raise FileNotFoundError(f"QGIS file not found: {qgz_path}")
    
    if target_layer_name is None:
        target_layer_name = "Capability model output"
    
    # Create temporary directory for extraction
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        
        # Extract the .qgz file (which is a ZIP)
        with zipfile.ZipFile(qgz_path, 'r') as z:
            z.extractall(tmpdir)
        
        # Find and parse .qgs file (look for any .qgs in the archive)
        qgs_files = list(tmpdir.glob("*.qgs"))
        if not qgs_files:
            raise FileNotFoundError(f"No .qgs file found in {qgz_path}")
        
        project_file = qgs_files[0]
        
        # Register namespace to preserve prefixes
        ET.register_namespace('', 'http://www.qgis.org/schema')
        
        tree = ET.parse(project_file)
        root = tree.getroot()
        
        # Find the target layer and get the data source
        layer_extent = _find_layer_extent_from_file(root, qgz_path.parent, target_layer_name)
        if layer_extent is None:
            raise ValueError(f"Layer '{target_layer_name}' not found or has no extent")
        
        # Update the canvas/map extent in the project
        _set_canvas_extent(root, layer_extent)
        
        # Write back the modified project.qgs
        tree.write(project_file, encoding='utf-8', xml_declaration=True)
        
        # Repackage as .qgz (ZIP)
        # Remove the old file and create a new one
        qgz_path.unlink()
        with zipfile.ZipFile(qgz_path, 'w', zipfile.ZIP_DEFLATED) as z:
            for file in tmpdir.rglob('*'):
                if file.is_file():
                    arcname = file.relative_to(tmpdir)
                    z.write(file, arcname)
        
        print(f"Updated {qgz_path} to zoom to layer '{target_layer_name}'")
        print(f"Extent: {layer_extent}")


def _find_layer_extent_from_file(root: ET.Element, project_dir: Path, layer_name: str) -> dict | None:
    """Find the spatial extent of a named layer by reading its data source.
    
    Inputs:
    - root: XML root element of project.qgs
    - project_dir: directory where the project file is located (for resolving relative paths)
    - layer_name: name of the layer to find
    
    Outputs:
    - dict with keys 'xmin', 'ymin', 'xmax', 'ymax' or None if not found
    """
    import sqlite3
    
    # Find maplayer with matching name
    projectlayers = root.find('projectlayers')
    if projectlayers is None:
        return None
    
    for maplayer in projectlayers.findall('maplayer'):
        layername = maplayer.find('layername')
        if layername is not None and layername.text == layer_name:
            # Found the layer, now get its data source
            datasource = maplayer.find('datasource')
            if datasource is None:
                return None
            
            source_str = datasource.text or ""
            
            # Parse the datasource string (format: path|layername=...)
            parts = source_str.split('|')
            file_path = parts[0] if parts else ""
            
            # Make relative path absolute
            if file_path and not Path(file_path).is_absolute():
                file_path = project_dir / file_path
            
            if not file_path or not Path(file_path).exists():
                print(f"Warning: Data source file not found: {file_path}")
                return None
            
            # Extract layer name from datasource if present
            layer_filter = ""
            for part in parts[1:]:
                if part.startswith("layername="):
                    layer_filter = part.split("=", 1)[1]
                    break
            
            # Try to read bounds from GeoPackage or Shapefile
            try:
                bounds = _get_layer_bounds(str(file_path), layer_filter)
                if bounds:
                    return bounds
            except Exception as e:
                print(f"Warning: Could not read bounds from {file_path}: {e}")
    
    return None


def _get_layer_bounds(file_path: str, layer_name: str = "") -> dict | None:
    """Get the bounds of a layer in a GeoPackage or Shapefile.
    
    Inputs:
    - file_path: path to .gpkg or .shp file
    - layer_name: name of the layer (for .gpkg files)
    
    Outputs:
    - dict with keys 'xmin', 'ymin', 'xmax', 'ymax' or None
    """
    file_path = Path(file_path)
    
    if file_path.suffix.lower() == '.gpkg':
        return _get_gpkg_bounds(str(file_path), layer_name)
    elif file_path.suffix.lower() == '.shp':
        return _get_shapefile_bounds(str(file_path))
    
    return None


def _get_gpkg_bounds(gpkg_path: str, layer_name: str = "") -> dict | None:
    """Get bounds from a GeoPackage layer.
    
    Inputs:
    - gpkg_path: path to .gpkg file
    - layer_name: table name in the GeoPackage (if empty, uses first features table)
    
    Outputs:
    - dict with bounds or None
    """
    try:
        import sqlite3
        conn = sqlite3.connect(gpkg_path)
        cursor = conn.cursor()
        
        # If no layer name specified, find the first features table
        if not layer_name:
            cursor.execute("SELECT table_name FROM gpkg_contents WHERE data_type='features' LIMIT 1")
            result = cursor.fetchone()
            if result:
                layer_name = result[0]
            else:
                conn.close()
                return None
        
        # Query the gpkg_contents table for extent
        query = """
            SELECT min_x, min_y, max_x, max_y 
            FROM gpkg_contents 
            WHERE table_name = ?
        """
        cursor.execute(query, (layer_name,))
        result = cursor.fetchone()
        conn.close()
        
        if result:
            min_x, min_y, max_x, max_y = result
            return {
                'xmin': float(min_x),
                'ymin': float(min_y),
                'xmax': float(max_x),
                'ymax': float(max_y),
            }
    except Exception as e:
        print(f"Error reading GeoPackage bounds: {e}")
    
    return None


def _get_shapefile_bounds(shp_path: str) -> dict | None:
    """Get bounds from a Shapefile.
    
    Inputs:
    - shp_path: path to .shp file
    
    Outputs:
    - dict with bounds or None
    """
    try:
        import struct
        with open(shp_path, 'rb') as f:
            # Shapefile header format
            f.seek(36)  # Skip to xmin position
            xmin = struct.unpack('>d', f.read(8))[0]  # Big-endian double
            ymin = struct.unpack('>d', f.read(8))[0]
            xmax = struct.unpack('>d', f.read(8))[0]
            ymax = struct.unpack('>d', f.read(8))[0]
            
            return {
                'xmin': xmin,
                'ymin': ymin,
                'xmax': xmax,
                'ymax': ymax,
            }
    except Exception as e:
        print(f"Error reading Shapefile bounds: {e}")
    
    return None


def _set_canvas_extent(root: ET.Element, extent: dict) -> None:
    """Set the canvas (map view) extent in the project.
    
    Inputs:
    - root: XML root element of project.qgs
    - extent: dict with keys 'xmin', 'ymin', 'xmax', 'ymax'
    
    Outputs:
    - None. Modifies root in place.
    """
    # In QGIS 3.40+, add a project macro that zooms to the layer on project load
    # Find or create the projectMacros element
    macros = root.find('Macros')
    if macros is None:
        macros = ET.Element('Macros')
        root.append(macros)
    
    # Remove any existing code
    for child in list(macros):
        macros.remove(child)
    
    # Add Python code that zooms to the capability layer dynamically
    python_code = """import qgis
from qgis.core import QgsRectangle, QgsProject

def project_load():
    canvas = iface.mapCanvas()
    # Find the Capability model output layer and zoom to its extent
    project = QgsProject.instance()
    for layer in project.mapLayers().values():
        if 'Capability' in layer.name() or 'capability' in layer.name().lower():
            if hasattr(layer, 'extent'):
                extent = layer.extent()
                canvas.setExtent(extent)
                canvas.refresh()
                break
"""
    
    # Create the code element
    code_elem = ET.SubElement(macros, 'pythonCode')
    code_elem.text = python_code
    
    print(f"Added dynamic project macro to zoom to capability layer")
    print(f"(Will automatically detect layer bounds at project load time)")


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print("Usage: python qgis_zoom_to_layer.py <qgz_path> [layer_name]")
        print("Example: python qgis_zoom_to_layer.py capability.qgz 'Capability model output'")
        sys.exit(1)
    
    qgz_file = sys.argv[1]
    layer = sys.argv[2] if len(sys.argv) > 2 else None
    zoom_to_layer(qgz_file, layer)
