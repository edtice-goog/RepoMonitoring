@echo off
REM ===========================================================================
REM Clean-builds the Subversion dependency stack from source into ONE prefix,
REM then builds svn against it. Same dual use as the git script: standalone
REM validation, or blackduck-c-cpp build_cmd (vcvars inherited).
REM
REM Resulting SBOM (9): subversion, apr, apr-util, zlib, openssl, expat,
REM                     sqlite, lz4, utf8proc
REM   - serf is OMITTED (SCons-only); OpenSSL stays in the BOM via apr-util's
REM     APU_HAVE_CRYPTO instead of via serf.
REM   - lz4 and utf8proc are VENDORED inside subversion/libsvn_subr/ and are
REM     always built from the internal copy on Windows - no separate build.
REM ===========================================================================
setlocal

where cl.exe >nul 2>&1 || call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat" >nul
where cl.exe >nul 2>&1 || ( echo [FATAL] cl.exe unavailable & exit /b 1 )

set "PATH=C:\Strawberry\perl\bin;C:\Users\EdTice\AppData\Local\bin\NASM;%PATH%"
set "TCLSH=C:\PROGRA~1\Git\mingw64\bin\tclsh.exe"

set "WS=C:\Data\repo-monitoring-workspace"
set "NEWSRC=%WS%\src"
set "OLDSRC=%WS%\stage3\src"
set "PREFIX=%WS%\prefix-svn"
set "PFX=C:/Data/repo-monitoring-workspace/prefix-svn"
set "POLICY=-DCMAKE_POLICY_VERSION_MINIMUM=3.5"
set "OSSLSYSLIBS=ws2_32.lib crypt32.lib advapi32.lib user32.lib gdi32.lib"

echo === [stack] wiping prefix ===
rmdir /s /q "%PREFIX%" 2>nul
mkdir "%PREFIX%" 2>nul

REM --------------------------------------------------------------- zlib -----
echo.
echo === [1/7] zlib ===
rmdir /s /q "%OLDSRC%\zlib\build-svn" 2>nul
cmake -S "%OLDSRC%\zlib" -B "%OLDSRC%\zlib\build-svn" -G Ninja %POLICY% ^
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=%PFX% || exit /b 1
cmake --build "%OLDSRC%\zlib\build-svn" --target install || exit /b 1

REM ------------------------------------------------------------ openssl -----
echo.
echo === [2/7] openssl (long) ===
cd /d "%OLDSRC%\openssl" || exit /b 1
if exist makefile nmake clean >nul 2>&1
perl Configure VC-WIN64A no-shared no-tests no-docs --prefix="%PREFIX%" --openssldir="%PREFIX%\ssl" || exit /b 1
nmake || exit /b 1
nmake install_sw || exit /b 1

REM -------------------------------------------------------------- expat -----
echo.
echo === [3/7] expat ===
rmdir /s /q "%NEWSRC%\libexpat\expat\build-svn" 2>nul
cmake -S "%NEWSRC%\libexpat\expat" -B "%NEWSRC%\libexpat\expat\build-svn" -G Ninja %POLICY% ^
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=%PFX% ^
  -DEXPAT_SHARED_LIBS=OFF -DEXPAT_BUILD_TESTS=OFF -DEXPAT_BUILD_EXAMPLES=OFF ^
  -DEXPAT_BUILD_TOOLS=OFF -DEXPAT_BUILD_DOCS=OFF || exit /b 1
cmake --build "%NEWSRC%\libexpat\expat\build-svn" --target install || exit /b 1
REM SVN's gen-make insists on libexpat.lib (or xml.lib); expat's MSVC CMake
REM emits libexpatMD.lib / libexpatMT.lib. Normalise the name.
if not exist "%PREFIX%\lib\libexpat.lib" (
  for %%F in ("%PREFIX%\lib\libexpatM*.lib") do copy /y "%%F" "%PREFIX%\lib\libexpat.lib" >nul
)
if not exist "%PREFIX%\lib\libexpat.lib" ( echo [FATAL] no libexpat.lib produced & dir /b "%PREFIX%\lib" & exit /b 1 )

REM ---------------------------------------------------------------- apr -----
echo.
echo === [4/7] apr ===
rmdir /s /q "%NEWSRC%\apr\build" 2>nul
cmake -S "%NEWSRC%\apr" -B "%NEWSRC%\apr\build" -G Ninja %POLICY% ^
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=%PFX% ^
  -DAPR_BUILD_TESTAPR=OFF -DINSTALL_PDB=OFF || exit /b 1
cmake --build "%NEWSRC%\apr\build" --target install || exit /b 1

REM ----------------------------------------------------------- apr-util -----
echo.
echo === [5/7] apr-util (APU_HAVE_CRYPTO=ON keeps OpenSSL in the BOM) ===
rmdir /s /q "%NEWSRC%\apr-util\build" 2>nul
cmake -S "%NEWSRC%\apr-util" -B "%NEWSRC%\apr-util\build" -G Ninja %POLICY% ^
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=%PFX% ^
  -DCMAKE_PREFIX_PATH=%PFX% -DOPENSSL_ROOT_DIR=%PFX% -DOPENSSL_USE_STATIC_LIBS=ON ^
  -DAPU_HAVE_CRYPTO=ON -DAPU_HAVE_ODBC=OFF -DAPR_HAS_LDAP=OFF ^
  -DAPR_BUILD_TESTAPR=OFF -DINSTALL_PDB=OFF ^
  -DCMAKE_SHARED_LINKER_FLAGS="%OSSLSYSLIBS%" ^
  -DCMAKE_EXE_LINKER_FLAGS="%OSSLSYSLIBS%" || exit /b 1
cmake --build "%NEWSRC%\apr-util\build" --target install || exit /b 1

REM ------------------------------------------------------------- sqlite -----
echo.
echo === [6/7] sqlite amalgamation (generated with tclsh) ===
cd /d "%NEWSRC%\sqlite" || exit /b 1
nmake /f Makefile.msc clean TCLSH_CMD=%TCLSH% >nul 2>&1
nmake /f Makefile.msc sqlite3.c TCLSH_CMD=%TCLSH% || exit /b 1
rmdir /s /q "%NEWSRC%\subversion\sqlite-amalgamation" 2>nul
mkdir "%NEWSRC%\subversion\sqlite-amalgamation" || exit /b 1
copy /y sqlite3.c "%NEWSRC%\subversion\sqlite-amalgamation\" >nul || exit /b 1
copy /y sqlite3.h "%NEWSRC%\subversion\sqlite-amalgamation\" >nul || exit /b 1
copy /y sqlite3ext.h "%NEWSRC%\subversion\sqlite-amalgamation\" >nul 2>&1

REM --------------------------------------------------------- subversion -----
echo.
echo === [7/7] subversion (gen-make + msbuild) ===
cd /d "%NEWSRC%\subversion" || exit /b 1
python gen-make.py -t vcproj --vsnet-version=2019 --release ^
  --with-apr="%PREFIX%" --with-apr-util="%PREFIX%" ^
  --with-zlib="%PREFIX%" --with-sqlite="%NEWSRC%\subversion\sqlite-amalgamation" || exit /b 1
msbuild subversion_vcnet.sln /p:Configuration=Release /p:Platform=x64 /p:PlatformToolset=v143 /m /v:minimal || exit /b 1

echo.
echo === [stack] build complete ===
endlocal
