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
        
        # Print root and first-level elements
        print(f"Root tag: {root.tag}")
        print(f"Root attributes: {root.attrib}\n")
        
        print("First-level elements:")
        for elem in root:
            print(f"  {elem.tag}: {len(elem)} children")
            
            # Check ProjectViewSettings
        pvs = root.find('ProjectViewSettings')
        if pvs is not None:
            print("\nProjectViewSettings:")
            print(f"  Attributes: {pvs.attrib}")
            for child in pvs:
                print(f"  {child.tag}: {child.attrib}")
                for grandchild in child:
                    print(f"    {grandchild.tag}: {grandchild.attrib}")
