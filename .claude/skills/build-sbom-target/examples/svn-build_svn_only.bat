@echo off
REM Step 7 only: regenerate SVN projects and build. Everything else is already
REM installed in prefix-svn and sqlite-amalgamation/ is already populated.
setlocal
where cl.exe >nul 2>&1 || call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat" >nul
set "NEWSRC=C:\Data\repo-monitoring-workspace\src"
set "PREFIX=C:\Data\repo-monitoring-workspace\prefix-svn"
cd /d "%NEWSRC%\subversion" || exit /b 1
python gen-make.py -t vcproj --vsnet-version=2019 --release ^
  --with-apr="%PREFIX%" --with-apr-util="%PREFIX%" ^
  --with-zlib="%PREFIX%" --with-sqlite="%NEWSRC%\subversion\sqlite-amalgamation" || exit /b 1
msbuild subversion_vcnet.sln /p:Configuration=Release /p:Platform=x64 /p:PlatformToolset=v143 /m /v:minimal || exit /b 1
echo === svn build OK ===
endlocal
