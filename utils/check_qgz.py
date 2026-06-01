import zipfile

z = zipfile.ZipFile(r'c:\Users\mocci\Desktop\PhD\II\CapabilityModel\outputs\qgis\Cagliari_Shapefile\capability.qgz')
print("Files in .qgz archive:")
for name in z.namelist():
    print(repr(name))
z.close()
