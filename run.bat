@echo off
rem ============================================================
rem  Flowza — визуальный конструктор автоматизаций без кода
rem  Запуск: Python 3 + Flask (pip install -r requirements.txt)
rem ============================================================
setlocal
cd /d "%~dp0"

rem Ищем python в PATH или в стандартных местах
set "PY=python"
where python >nul 2>nul
if errorlevel 1 (
    where py >nul 2>nul
    if not errorlevel 1 (
        set "PY=py -3"
    )
)

%PY% -c "import flask" >nul 2>nul
if errorlevel 1 (
    echo.
    echo  [Flowza] Не найдена зависимость Flask.
    echo  Установите её командой:  %PY% -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

echo.
echo  [Flowza] Запуск редактора...
echo  [Flowza] Если браузер не откроется, перейдите на http://127.0.0.1:8765
echo  [Flowza] Остановка: закройте это окно или нажмите Ctrl+C
echo.

%PY% main.py

if errorlevel 1 (
    echo.
    echo  [Flowza] Ошибка запуска. Проверьте сообщение выше.
    echo  Если Python не установлен: https://python.org
    echo.
    pause
)
endlocal
