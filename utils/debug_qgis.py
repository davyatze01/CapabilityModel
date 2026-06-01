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
        
        # Find projectlayers
        projectlayers = root.find('projectlayers')
        if projectlayers:
            print(f"\nFound projectlayers")
            for maplayer in projectlayers.findall('maplayer'):
                print(f"\nMapLayer:")
                layername = maplayer.find('layername')
                if layername is not None:
                    print(f"  Name: {layername.text}")
                
                datasource = maplayer.find('datasource')
                if datasource is not None:
                    print(f"  Datasource: {datasource.text[:100]}")
        else:
            print("projectlayers not found")
