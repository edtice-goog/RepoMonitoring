@echo off
REM Retry ONLY the git step (deps already installed in the prefix).
REM Adds /DCURL_STATICLIB and /DXML_STATIC globally: git compiles http.c into a
REM standalone OBJECT library (http_obj) that never links CURL::libcurl, so the
REM target's INTERFACE_COMPILE_DEFINITIONS never reach it. Same for static expat.
setlocal
where cl.exe >nul 2>&1 || call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat" >nul
set "PATH=C:\Strawberry\perl\bin;%PATH%"
set "NEWSRC=C:\Data\repo-monitoring-workspace\src"
set "PFX=C:/Data/repo-monitoring-workspace/prefix"
set "PKG_CONFIG_PATH=C:\Data\repo-monitoring-workspace\prefix\lib\pkgconfig"

rmdir /s /q "%NEWSRC%\git\contrib\buildsystems\out" 2>nul
cmake -S "%NEWSRC%\git\contrib\buildsystems" -B "%NEWSRC%\git\contrib\buildsystems\out" -G Ninja ^
  -DCMAKE_BUILD_TYPE=Release -DUSE_VCPKG=OFF ^
  -DCMAKE_PREFIX_PATH=%PFX% -DZLIB_ROOT=%PFX% -DZLIB_USE_STATIC_LIBS=ON ^
  -DBUILD_TESTING=OFF ^
  -DPKG_CONFIG_EXECUTABLE=C:/Strawberry/perl/bin/pkg-config.bat ^
  -DCMAKE_C_FLAGS="/DCURL_STATICLIB /DXML_STATIC /DPCRE2_STATIC" || exit /b 1
cmake --build "%NEWSRC%\git\contrib\buildsystems\out" --target git git-daemon git-http-backend git-sh-i18n--envsubst git-shell scalar git-imap-send git-http-fetch git-remote-http git-http-push headless-git || exit /b 1
echo === git build OK ===
endlocal
