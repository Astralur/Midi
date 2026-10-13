@echo off
setlocal
cd /d "%~dp0"
if errorlevel 1 goto :error

rem Reutilizar el entorno virtual; el mapa editable nunca se sobrescribe.
if exist ".venv\Scripts\python.exe" goto :check_dependency
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 goto :fallback_python
py -3 -m venv .venv
if errorlevel 1 goto :error
goto :check_dependency

:fallback_python
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 goto :missing_python
python -m venv .venv
if errorlevel 1 goto :error

:check_dependency
".venv\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 goto :old_environment
".venv\Scripts\python.exe" -c "import importlib.metadata as m; assert m.version('mido') == '1.3.3'" >nul 2>&1
if not errorlevel 1 goto :launch
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :error

:launch
".venv\Scripts\python.exe" songsterr_a_invasion.py --gui
if errorlevel 1 goto :error
exit /b 0

:missing_python
echo No se encontro Python 3.10 o posterior. Instala una version compatible con Tcl/Tk.
echo Activa la opcion para agregar Python al PATH y ejecuta este archivo otra vez.
pause
exit /b 1

:old_environment
echo El entorno .venv usa un Python anterior a 3.10 o no se puede ejecutar.
echo Renombra .venv e instala Python 3.10 o posterior antes de volver a iniciar.
pause
exit /b 1

:error
echo No se pudo preparar o iniciar la aplicacion. Revisa el error mostrado arriba.
pause
exit /b 1
