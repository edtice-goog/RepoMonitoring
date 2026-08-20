@echo off
REM ===========================================================================
REM Clean-builds the whole git dependency stack from source into ONE shared
REM prefix, then builds git against it.
REM
REM Used two ways:
REM   1. standalone validation  -> calls vcvars64 itself (cl not yet on PATH)
REM   2. as blackduck-c-cpp build_cmd -> vcvars already inherited, skips it
REM
REM Every dep is clean-built so Coverity captures each translation unit and the
REM component lands MONITORED rather than reference-only.
REM
REM Resulting SBOM (7): git, zlib, openssl, curl, expat, pcre2, win-iconv
REM   NOTE: git's CMake hard-defines NO_OPENSSL (uses sha1dc), so OpenSSL
REM   enters the BOM via curl, not via git directly.
REM ===========================================================================
setlocal

REM --- only enter the MSVC env if we are not already inside it ---------------
where cl.exe >nul 2>&1 || call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat" >nul
where cl.exe >nul 2>&1 || ( echo [FATAL] cl.exe unavailable & exit /b 1 )

set "PATH=C:\Strawberry\perl\bin;C:\Users\EdTice\AppData\Local\bin\NASM;%PATH%"

set "WS=C:\Data\repo-monitoring-workspace"
set "NEWSRC=%WS%\src"
set "OLDSRC=%WS%\stage3\src"
set "PREFIX=%WS%\prefix"
REM CMake needs forward slashes (backslashes escape inside try_compile)
set "PFX=C:/Data/repo-monitoring-workspace/prefix"
set "PKG_CONFIG_PATH=%PREFIX%\lib\pkgconfig"

REM CMake 4.x removed compatibility with cmake_minimum_required(<3.5);
REM win-iconv (2.6) and zlib (range from 2.4.4) need the escape hatch.
set "POLICY=-DCMAKE_POLICY_VERSION_MINIMUM=3.5"

echo === [stack] wiping prefix ===
rmdir /s /q "%PREFIX%" 2>nul
mkdir "%PREFIX%" 2>nul

REM --------------------------------------------------------------- zlib -----
echo.
echo === [1/7] zlib ===
rmdir /s /q "%OLDSRC%\zlib\build" 2>nul
cmake -S "%OLDSRC%\zlib" -B "%OLDSRC%\zlib\build" -G Ninja %POLICY% ^
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=%PFX% || exit /b 1
cmake --build "%OLDSRC%\zlib\build" --target install || exit /b 1

REM ------------------------------------------------------------ openssl -----
echo.
echo === [2/7] openssl (long) ===
cd /d "%OLDSRC%\openssl" || exit /b 1
if exist makefile nmake clean >nul 2>&1
perl Configure VC-WIN64A no-shared no-tests no-docs --prefix="%PREFIX%" --openssldir="%PREFIX%\ssl" || exit /b 1
nmake || exit /b 1
nmake install_sw || exit /b 1

REM --------------------------------------------------------------- curl -----
echo.
echo === [3/7] curl ===
rmdir /s /q "%OLDSRC%\curl\build" 2>nul
cmake -S "%OLDSRC%\curl" -B "%OLDSRC%\curl\build" -G Ninja %POLICY% ^
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=%PFX% ^
  -DBUILD_SHARED_LIBS=OFF -DBUILD_CURL_EXE=ON -DCURL_STATICLIB=ON ^
  -DCURL_USE_SCHANNEL=OFF -DCURL_USE_OPENSSL=ON -DOPENSSL_USE_STATIC_LIBS=ON ^
  -DOPENSSL_ROOT_DIR=%PFX% ^
  -DCURL_ZLIB=ON -DZLIB_INCLUDE_DIR=%PFX%/include -DZLIB_LIBRARY=%PFX%/lib/zlibstatic.lib ^
  -DCURL_USE_LIBPSL=OFF -DCURL_USE_LIBSSH2=OFF -DUSE_NGHTTP2=OFF ^
  -DCURL_USE_LIBIDN2=OFF -DUSE_LIBIDN2=OFF -DCURL_BROTLI=OFF -DCURL_ZSTD=OFF ^
  -DCURL_USE_GSSAPI=OFF -DUSE_WIN32_IDN=ON -DCURL_DISABLE_TESTS=ON || exit /b 1
cmake --build "%OLDSRC%\curl\build" --target install || exit /b 1

REM -------------------------------------------------------------- expat -----
echo.
echo === [4/7] expat ===
rmdir /s /q "%NEWSRC%\libexpat\expat\build" 2>nul
cmake -S "%NEWSRC%\libexpat\expat" -B "%NEWSRC%\libexpat\expat\build" -G Ninja %POLICY% ^
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=%PFX% ^
  -DEXPAT_SHARED_LIBS=OFF -DEXPAT_BUILD_TESTS=OFF -DEXPAT_BUILD_EXAMPLES=OFF ^
  -DEXPAT_BUILD_TOOLS=OFF -DEXPAT_BUILD_DOCS=OFF || exit /b 1
cmake --build "%NEWSRC%\libexpat\expat\build" --target install || exit /b 1

REM -------------------------------------------------------------- pcre2 -----
echo.
echo === [5/7] pcre2 ===
rmdir /s /q "%NEWSRC%\pcre2\build" 2>nul
cmake -S "%NEWSRC%\pcre2" -B "%NEWSRC%\pcre2\build" -G Ninja %POLICY% ^
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=%PFX% ^
  -DBUILD_SHARED_LIBS=OFF -DPCRE2_BUILD_PCRE2_8=ON ^
  -DPCRE2_BUILD_PCRE2GREP=OFF -DPCRE2_BUILD_TESTS=OFF || exit /b 1
cmake --build "%NEWSRC%\pcre2\build" --target install || exit /b 1
REM pkg-config emits -lpcre2-8 but the static build installs pcre2-8-static.lib.
if not exist "%PREFIX%\lib\pcre2-8.lib" copy /y "%PREFIX%\lib\pcre2-8-static.lib" "%PREFIX%\lib\pcre2-8.lib" >nul
if not exist "%PREFIX%\lib\pcre2-8.lib" ( echo [FATAL] no pcre2-8.lib produced & exit /b 1 )

REM ---------------------------------------------------------- win-iconv -----
echo.
echo === [6/7] win-iconv ===
rmdir /s /q "%NEWSRC%\win-iconv\build" 2>nul
cmake -S "%NEWSRC%\win-iconv" -B "%NEWSRC%\win-iconv\build" -G Ninja %POLICY% ^
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=%PFX% ^
  -DBUILD_STATIC=ON -DBUILD_SHARED=OFF -DBUILD_EXECUTABLE=OFF -DBUILD_TEST=OFF || exit /b 1
cmake --build "%NEWSRC%\win-iconv\build" --target install || exit /b 1

REM ----------------------------------------------------------------- git -----
echo.
echo === [7/7] git ===
rmdir /s /q "%NEWSRC%\git\contrib\buildsystems\out" 2>nul
cmake -S "%NEWSRC%\git\contrib\buildsystems" -B "%NEWSRC%\git\contrib\buildsystems\out" -G Ninja ^
  -DCMAKE_BUILD_TYPE=Release -DUSE_VCPKG=OFF ^
  -DCMAKE_PREFIX_PATH=%PFX% -DZLIB_ROOT=%PFX% -DZLIB_USE_STATIC_LIBS=ON ^
  -DBUILD_TESTING=OFF ^
  -DPKG_CONFIG_EXECUTABLE=C:/Strawberry/perl/bin/pkg-config.bat ^
  -DCMAKE_C_FLAGS="/DCURL_STATICLIB /DXML_STATIC /DPCRE2_STATIC" || exit /b 1
cmake --build "%NEWSRC%\git\contrib\buildsystems\out" --target git git-daemon git-http-backend git-sh-i18n--envsubst git-shell scalar git-imap-send git-http-fetch git-remote-http git-http-push headless-git || exit /b 1

echo.
echo === [stack] build complete ===
endlocal
