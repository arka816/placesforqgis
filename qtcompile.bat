@echo off
call "C:\Program Files\QGIS 3.22.7\bin\o4w_env.bat"

@echo on
pyrcc5 -o resources.py resources.qrc
pause