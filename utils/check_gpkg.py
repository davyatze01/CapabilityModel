import sqlite3
from pathlib import Path

gpkg_path = Path(r'c:\Users\mocci\Desktop\PhD\II\CapabilityModel\outputs\qgis\Cagliari_Shapefile\..\..\gpkg\Cagliari_Shapefile\Cagliari_Shapefile.gpkg')
gpkg_path = gpkg_path.resolve()

print(f"GeoPackage path: {gpkg_path}")
print(f"Exists: {gpkg_path.exists()}")

if gpkg_path.exists():
    conn = sqlite3.connect(str(gpkg_path))
    cursor = conn.cursor()
    
    # Get all tables
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = cursor.fetchall()
    print(f"\nTables in GeoPackage:")
    for table in tables:
        print(f"  {table[0]}")
    
    # Get gpkg_contents
    print(f"\ngpkg_contents:")
    cursor.execute("PRAGMA table_info(gpkg_contents)")
    columns = cursor.fetchall()
    print(f"  Columns: {[col[1] for col in columns]}")
    
    cursor.execute("SELECT * FROM gpkg_contents")
    for row in cursor.fetchall():
        print(f"  {row}")
    
    conn.close()
