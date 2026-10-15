@echo off
rem Builds dist\ClearSpace\ClearSpace.exe and dist\ClearSpace-2.0.0-win64.zip for Windows 10/11.
setlocal
cd /d "%~dp0"

python -m pip install --require-hashes --no-deps -r requirements.lock || goto :error
python -m pytest -q || goto :error
python -m PyInstaller --noconfirm --clean ClearSpace.spec || goto :error

powershell -NoProfile -Command "Compress-Archive -Path 'dist\ClearSpace\*' -DestinationPath 'dist\ClearSpace-2.0.0-win64.zip' -Force; $h=(Get-FileHash -Algorithm SHA256 'dist\ClearSpace-2.0.0-win64.zip').Hash.ToLower(); Set-Content -Encoding ascii 'dist\SHA256SUMS' ($h + '  ClearSpace-2.0.0-win64.zip')" || goto :error
rmdir /s /q build 2>nul

echo.
echo Build OK: dist\ClearSpace\ClearSpace.exe
echo Archive : dist\ClearSpace-2.0.0-win64.zip
echo Checksum: dist\SHA256SUMS
exit /b 0

:error
echo Build FAILED (exit code %errorlevel%).
exit /b 1
