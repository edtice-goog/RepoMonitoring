@echo off
REM Resume the SVN stack at step 5 (zlib/openssl/expat/apr already in prefix-svn).
REM Static libcrypto pulls in Win32 system libs that apr-util's CMake omits:
REM   crypt32 -> CertOpenStore/CertCloseStore ; ws2_32 -> recv/send/WSA*
setlocal
where cl.exe >nul 2>&1 || call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat" >nul
set "PATH=C:\Strawberry\perl\bin;C:\Users\EdTice\AppData\Local\bin\NASM;%PATH%"
set "TCLSH=C:\PROGRA~1\Git\mingw64\bin\tclsh.exe"
set "NEWSRC=C:\Data\repo-monitoring-workspace\src"
set "PREFIX=C:\Data\repo-monitoring-workspace\prefix-svn"
set "PFX=C:/Data/repo-monitoring-workspace/prefix-svn"
set "POLICY=-DCMAKE_POLICY_VERSION_MINIMUM=3.5"
set "OSSLSYSLIBS=ws2_32.lib crypt32.lib advapi32.lib user32.lib gdi32.lib"

echo === [5/7] apr-util ===
rmdir /s /q "%NEWSRC%\apr-util\build" 2>nul
cmake -S "%NEWSRC%\apr-util" -B "%NEWSRC%\apr-util\build" -G Ninja %POLICY% ^
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=%PFX% ^
  -DCMAKE_PREFIX_PATH=%PFX% -DOPENSSL_ROOT_DIR=%PFX% -DOPENSSL_USE_STATIC_LIBS=ON ^
  -DAPU_HAVE_CRYPTO=ON -DAPU_HAVE_ODBC=OFF -DAPR_HAS_LDAP=OFF ^
  -DAPR_BUILD_TESTAPR=OFF -DINSTALL_PDB=OFF ^
  -DCMAKE_SHARED_LINKER_FLAGS="%OSSLSYSLIBS%" ^
  -DCMAKE_EXE_LINKER_FLAGS="%OSSLSYSLIBS%" || exit /b 1
cmake --build "%NEWSRC%\apr-util\build" --target install || exit /b 1

echo === [6/7] sqlite amalgamation ===
cd /d "%NEWSRC%\sqlite" || exit /b 1
nmake /f Makefile.msc clean TCLSH_CMD=%TCLSH% >nul 2>&1
nmake /f Makefile.msc sqlite3.c TCLSH_CMD=%TCLSH% || exit /b 1
rmdir /s /q "%NEWSRC%\subversion\sqlite-amalgamation" 2>nul
mkdir "%NEWSRC%\subversion\sqlite-amalgamation" || exit /b 1
copy /y sqlite3.c "%NEWSRC%\subversion\sqlite-amalgamation\" >nul || exit /b 1
copy /y sqlite3.h "%NEWSRC%\subversion\sqlite-amalgamation\" >nul || exit /b 1
copy /y sqlite3ext.h "%NEWSRC%\subversion\sqlite-amalgamation\" >nul 2>&1

echo === [7/7] subversion ===
cd /d "%NEWSRC%\subversion" || exit /b 1
python gen-make.py -t vcproj --vsnet-version=2019 --release ^
  --with-apr="%PREFIX%" --with-apr-util="%PREFIX%" ^
  --with-zlib="%PREFIX%" --with-sqlite="%NEWSRC%\subversion\sqlite-amalgamation" || exit /b 1
msbuild subversion_vcnet.sln /p:Configuration=Release /p:Platform=x64 /p:PlatformToolset=v143 /m /v:minimal || exit /b 1

echo === svn stack build complete ===
endlocal
