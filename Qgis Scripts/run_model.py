import sys
import os
from pathlib import Path

os.chdir(r"C:\Users\mocci\Desktop\PhD\II\CapabilityModel")
project_path = Path(r"C:\Users\mocci\Desktop\PhD\II\CapabilityModel")
sys.path.append(str(project_path))

from qgis_entrypoint import run_capability_model_from_qgis

result = run_capability_model_from_qgis()
print(result)