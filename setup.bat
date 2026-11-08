@echo off
rem Windows one-click setup entry. All logic lives in setup.ps1
rem (avoids CMD codepage issues with Chinese output).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
