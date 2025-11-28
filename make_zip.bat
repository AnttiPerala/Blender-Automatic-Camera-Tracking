@echo off
REM --- Set output zip name ---
set ZIP_NAME=automatic_camera_tracker.zip

REM --- Delete existing zip if any ---
if exist "%ZIP_NAME%" del "%ZIP_NAME%"

REM --- Use PowerShell's built-in Compress-Archive to zip the files ---
powershell -Command "Compress-Archive -Path '__init__.py','blender_manifest.toml' -DestinationPath '%ZIP_NAME%' -Force"

echo Created %ZIP_NAME%
pause
