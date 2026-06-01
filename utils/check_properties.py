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
        
        tree = ET.parse(project_file)
        root = tree.getroot()
        
        # Check properties
        props = root.find('properties')
        if props is not None:
            print("Properties element found:")
            for child in props:
                print(f"  {child.tag}:")
                for elem in child:
                    print(f"    {elem.tag}: {elem.text if elem.text else elem.attrib}")
