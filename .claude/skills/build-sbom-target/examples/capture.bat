@echo off
REM Run blackduck-c-cpp for the git capture (MSVC - needs vcvars).
REM Token pulled from the gitignored blackduck.local.json (field-test instance)
REM so it never lands on disk or in a command line.
setlocal
call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat" >nul

set "CFG=C:\Data\repo-monitoring-workspace\RepoMonitoring\blackduck.local.json"
for /f "delims=" %%T in ('python -c "import json;print(json.load(open(r'%CFG%',encoding='utf-8-sig'))['api_token'])"') do set "BLACKDUCK_API_TOKEN=%%T"
if "%BLACKDUCK_API_TOKEN%"=="" ( echo could not read api_token & exit /b 1 )

cd /d C:\Data\repo-monitoring-workspace\git-capture
echo === running blackduck-c-cpp ===
C:\Data\repo-monitoring-workspace\stage3\.venv\Scripts\blackduck-c-cpp.exe --config config.yaml
echo === blackduck-c-cpp exit code: %errorlevel% ===
endlocal
