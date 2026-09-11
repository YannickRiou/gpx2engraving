@echo off
REM Lanceur gpx2engraving : utilise le Python fourni avec QGIS (GDAL, shapely, pyproj, fontTools...).
setlocal
set "QGIS_PY="
if exist "C:\OSGeo4W\bin\python-qgis.bat" set "QGIS_PY=C:\OSGeo4W\bin\python-qgis.bat"
for /d %%D in ("C:\Program Files\QGIS 3*") do (
    if exist "%%D\bin\python-qgis-ltr.bat" set "QGIS_PY=%%D\bin\python-qgis-ltr.bat"
    if exist "%%D\bin\python-qgis.bat" set "QGIS_PY=%%D\bin\python-qgis.bat"
)
if defined GPX2ENGRAVING_PYTHON set "QGIS_PY=%GPX2ENGRAVING_PYTHON%"
if not defined QGIS_PY (
    echo Python QGIS introuvable. Installez QGIS ou definissez GPX2ENGRAVING_PYTHON.
    exit /b 1
)
call "%QGIS_PY%" "%~dp0gpx2engraving.py" %*
