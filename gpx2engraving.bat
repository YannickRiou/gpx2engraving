@echo off
REM gpx2engraving launcher: runs the script with the Python bundled with QGIS (GDAL, shapely, pyproj, fontTools...).
setlocal
set "QGIS_PY="
if exist "C:\OSGeo4W\bin\python-qgis.bat" set "QGIS_PY=C:\OSGeo4W\bin\python-qgis.bat"
for /d %%D in ("C:\Program Files\QGIS 3*") do (
    if exist "%%D\bin\python-qgis-ltr.bat" set "QGIS_PY=%%D\bin\python-qgis-ltr.bat"
    if exist "%%D\bin\python-qgis.bat" set "QGIS_PY=%%D\bin\python-qgis.bat"
)
if defined GPX2ENGRAVING_PYTHON set "QGIS_PY=%GPX2ENGRAVING_PYTHON%"
if not defined QGIS_PY (
    echo QGIS Python not found. Install QGIS or set GPX2ENGRAVING_PYTHON to a python.exe that has the dependencies.
    exit /b 1
)
call "%QGIS_PY%" "%~dp0gpx2engraving.py" %*
