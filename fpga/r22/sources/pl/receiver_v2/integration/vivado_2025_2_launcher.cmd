@echo off
rem Launch Vivado 2025.2 without relying on vivado.bat's caller-CWD pushd.
rem The board builder invokes this file from an isolated frozen source tree.
setlocal
rem The sandboxed shell cannot create Vivado's per-user app data below C:\.
rem Keep this process-local cache in the workspace; it is not a build input.
set "APPDATA=G:\fpga_transformer\zedboard_rf_migration\build\vivado_user_data"
set "LOCALAPPDATA=G:\fpga_transformer\zedboard_rf_migration\build\vivado_user_data"
if not exist "%APPDATA%" mkdir "%APPDATA%"
call "G:\AMDDesignTools\2025.2\Vivado\bin\setupEnv.bat"
call "G:\AMDDesignTools\2025.2\Vivado\bin\loader.bat" -exec vivado %*
exit /b %errorlevel%
