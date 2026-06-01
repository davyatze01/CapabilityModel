import zipfile
import tempfile
from pathlib import Path
import xml.etree.ElementTree as ET

qgz_path = Path(r'c:\Users\mocci\Desktop\PhD\II\CapabilityModel\outputs\qgis\Cagliari_Shapefile\capability.qgz')

with tempfile.TemporaryDirectory() as tmpdir:
    tmpdir = Path(tmpdir)
    with zipfile.ZipFile(qgz_path, 'r') as z:
        z.extractall(tmpdir)
    
    qgs_files = list(tmpdir.glob("*.qgs"))
    if qgs_files:
        project_file = qgs_files[0]
        print(f"Reading {project_file.name}")
        
        tree = ET.parse(project_file)
        root = tree.getroot()
        
        # Find all map layers
        print("\nMap Layers in project:")
        
        # Try maplayer
        maplayersroot = root.find('projectlayers')
        if maplayersroot is not None:
            for i, maplayer in enumerate(maplayersroot.findall('maplayer')):
                name_elem = maplayer.find('layername')
                layer_name = name_elem.text if name_elem is not None else f"Unknown{i}"
                print(f"  {i}. {layer_name}")
                
                # Print layer attributes
                print(f"     Attribs: {maplayer.attrib}")
                
                # Check for extent
                extent = maplayer.find('.//extent')
                if extent is not None:
                    print(f"     Extent: {extent.attrib}")
        else:
            print("  No projectlayers found")
        
        # Also try searching for layer elements with type="vector" or "raster"
        print("\nAll layer-like elements:")
        for elem in root.iter():
            if 'layer' in elem.tag.lower() or elem.tag in ['maplayer', 'raster', 'vector']:
                print(f"  {elem.tag}: {elem.attrib}")
