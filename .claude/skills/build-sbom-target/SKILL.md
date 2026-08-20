---
name: build-sbom-target
description: Build a C/C++ application and all its third-party dependencies from source so a BD/CPP capture yields a multi-component (7-10 entry) SBOM with everything MONITORED rather than reference-only. Use when choosing or standing up a new demo/analysis target, when an SBOM comes back with too few components, or when components land reference-only instead of monitored.
---

# Build a multi-component SBOM target

`run-analysis` covers capturing and ingesting a target that already builds. This
skill covers the harder part: **choosing a target and getting its whole
dependency stack to clean-build from source in one command**, so the capture
produces a rich SBOM instead of one or two components.

## The invariant everything else follows from

> A component is MONITORED only if Coverity saw its translation units compile
> *during the capture*. Anything linked as a prebuilt `.lib`/`.dll` is
> reference-only.

So `build_cmd` must clean-build **every** dependency you want in the BOM, in one
script, into one shared install prefix. This is the opposite of normal practice
(vcpkg/system packages) and every trap below is some build system trying
helpfully to hand you a prebuilt dependency instead.

## 1. Choosing a target

Aim for an application whose dependencies are *compiled*, not just linked.
Count only deps that will actually build from source on your platform.

| Target | Components | Notes |
|---|---|---|
| **git** | **6 confirmed** (7 built) | Captured and confirmed in BD. Meta-appropriate for repo monitoring. MSVC via its own CMake. |
| **Subversion** | 9 built | Build validated; BD count not yet confirmed. Higher friction (`gen-make.py` + msbuild), but more components and far less audited than git. |
| libarchive kitchen-sink | ~8-9 | Not built here. Every component is a parser/decompressor — strongest CVE-triage narrative. |
| curl kitchen-sink | ~8-9 | Not built here. Lowest risk: extends `stage3/` by re-enabling its disabled `-DUSE_*=OFF` switches. |

Things that *reduce* the count, so check before committing:
- The app may hard-disable a dep. git's CMake sets `NO_OPENSSL` unconditionally
  (uses sha1dc), so OpenSSL enters git's BOM only via curl.
- Dropping one dep can silently drop another. Omitting SVN's serf removes its
  only OpenSSL consumer — `-DAPU_HAVE_CRYPTO=ON` on apr-util puts it back.
- **Vendored deps count and are free.** SVN always builds its internal
  `subversion/libsvn_subr/{lz4,utf8proc}` on Windows. They compile from source,
  so they land monitored with no separate build. Grep the tree for bundled
  copies before adding a dep build.
- **One BD component can cover several upstream repos.** APR and APR-util come
  back as two BoM rows with the same `componentName` ("Apache Portable Runtime")
  and the *same component UUID*, separated only by `origins[].externalId`
  (`apache/apr:1.7.6` vs `apache/apr-util:1.6.3`). Not CPE-driven — NVD gives
  APR-util its own `cpe:2.3:a:apache:portable_runtime_utility`. The merge shares
  one version namespace across two projects, so CVE-2017-12613 (APR,
  `apache:portable_runtime`, "< 1.7.0") is reported against the apr-util 1.6.3
  row while the real APR 1.7.6 is clean. **Count components by origin, not by
  name**, and expect the monitor to split them into separate watched repos —
  that split is a large part of its value over the BoM alone.
- **Building a dep does not guarantee BD identifies it — but the monitor may
  still recover it.** win-iconv (a single ~1,000-line `win_iconv.c`, ~1 real TU)
  built and linked correctly but did **not** appear in the BD BOM: too small to
  signature-match. The monitor still shows it, because ingest unions the BD BOM
  with `.git` provenance from the checkout. So git is **6 in Black Duck, 7 in
  the monitor**. Quote whichever number matches the audience, and keep `.git`
  intact precisely so this recovery can happen.

## 2. Always validate before capturing

Write the stack script so it runs standalone *and* as `build_cmd`:

```bat
where cl.exe >nul 2>&1 || call "...\vcvars64.bat" >nul
```

Then run it directly first. **A capture run costs hours** — cov-build
instrumentation runs roughly 4x the standalone build (OpenSSL alone: ~15 min
standalone vs 1h23m instrumented; the full git stack was 1h39m of build plus
~15 min of emit/scan/upload). A failed capture teaches you exactly what a
5-minute direct build does. Both stacks here needed **11 fixes** before they
built; none needed a capture to find.

Also write a **resume script** covering only the later steps once the early ones
install cleanly (`build_git_only.bat`, `build_svn_resume.bat`,
`build_svn_only.bat`). Rebuilding OpenSSL to test a msbuild flag wastes 20
minutes per iteration.

Never edit a `.bat` while it is executing — cmd reads batch files incrementally.

## 3. Gotcha catalogue

### Applies to any CMake dependency

- **CMake 4.x removed `cmake_minimum_required(<3.5)`.** Old deps fail to
  configure. Pass `-DCMAKE_POLICY_VERSION_MINIMUM=3.5`. Needed by: win-iconv
  (2.6), apr (2.8), apr-util (2.8), utf8proc (3.0). Not needed by expat (3.13),
  pcre2 (3.15), lz4 (3.5).
- **Static libs need their "I am static" define at compile time**, or headers
  declare `__declspec(dllimport)` and you get `__imp_*` link errors. The define
  rides on the imported *target*'s `INTERFACE_COMPILE_DEFINITIONS` — which does
  nothing if the consumer compiles that source into a separate OBJECT library
  that never links the target. Pass them globally instead:
  `-DCMAKE_C_FLAGS="/DCURL_STATICLIB /DXML_STATIC /DPCRE2_STATIC"`.
- **MSVC static libs get decorated names** that consumers don't expect. Copy to
  the expected name after install:
  - expat emits `libexpatMD.lib`; SVN's gen-make demands `libexpat.lib`
  - pcre2 emits `pcre2-8-static.lib`; pkg-config reports `-lpcre2-8`
- **`find_package(ZLIB)` prefers the shared import lib**, leaving binaries
  needing `zlib.dll` (exit 53, no output). Use `-DZLIB_USE_STATIC_LIBS=ON`.
- **CMake's `find_program` ignores `.bat` on Windows.** Strawberry ships
  `pkg-config.bat`, so `find_package(PkgConfig)` fails and any pkg-config-only
  dependency is silently skipped. Pass
  `-DPKG_CONFIG_EXECUTABLE=C:/Strawberry/perl/bin/pkg-config.bat` and set
  `PKG_CONFIG_PATH` to `<prefix>/lib/pkgconfig`.
- **Static OpenSSL needs Win32 system libs** its consumers often omit:
  `ws2_32.lib crypt32.lib advapi32.lib user32.lib gdi32.lib`
  (`crypt32` → `CertOpenStore`; `ws2_32` → `recv`/`send`/`WSA*`). Pass via
  `-DCMAKE_SHARED_LINKER_FLAGS` and `-DCMAKE_EXE_LINKER_FLAGS`.

### git specifically

- **`-DNO_VCPKG=TRUE` does nothing — the option is `USE_VCPKG`.** git's own
  comment (contrib/buildsystems/CMakeLists.txt:46) is stale. Use
  `-DUSE_VCPKG=OFF`. **This is the dangerous one**: vcpkg bootstraps silently,
  downloads prebuilt deps, and the capture *succeeds* with an SBOM that is
  entirely reference-only.
- **`-DBUILD_TESTING=OFF` breaks the `all` target.** `GIT-BUILD-OPTIONS` is
  generated only inside the `if(BUILD_TESTING)` block but perl-script targets
  (`git-archimport`) still depend on it. Build the C executables explicitly:
  ```
  --target git git-daemon git-http-backend git-sh-i18n--envsubst git-shell
           scalar git-imap-send git-http-fetch git-remote-http git-http-push
           headless-git
  ```
  (Leaving `BUILD_TESTING` on instead fails differently: the clar unit-test
  targets need generated `clar-decls.h`/`clar.suite` that race under ninja.)
- git resolves deps via `find_package` for ZLIB/CURL/EXPAT/Iconv but **PCRE2
  only through pkg-config** — see the `.bat` trap above.

### Subversion specifically

- **`--vsnet-version=2022` silently degrades to VS2012/v110** (MSB8020: build
  tools not found). `gen_win_dependencies.py` only maps through `2019`/`16`;
  anything else 20xx hits a catch-all that prints a warning and assumes 2012.
  Use `--vsnet-version=2019` and retarget at build time:
  `msbuild ... /p:PlatformToolset=v143`.
- **serf is SCons-only** (1.3.10) and SCons isn't typically installed. Omitting
  it costs http:// access and `ra_serf`; keep OpenSSL via apr-util crypto.
- **SQLite comes from the amalgamation.** Generate it from a GitHub clone:
  `nmake /f Makefile.msc sqlite3.c TCLSH_CMD=<tclsh>`, then copy `sqlite3.c`,
  `sqlite3.h`, `sqlite3ext.h` into `subversion/sqlite-amalgamation/`.
  - `Makefile.msc` pipes `$(TCLSH_CMD)` **unquoted**, so a path with spaces
    breaks it. Use the 8.3 short path: `C:\PROGRA~1\Git\mingw64\bin\tclsh.exe`
    (Git for Windows ships tclsh 8.6).
  - **Always `nmake /f Makefile.msc clean` first.** A failed run leaves a
    truncated `opcodes.h`; nmake then thinks it is up to date and
    `mkopcodec.tcl` dies with `can't read "label(0)"`.
  - A complete `sqlite3.c` is ~260k lines / ~9 MB and ends with
    `End of sqlite3.c`. Verify before trusting it.
- apr-util defaults `APR_INCLUDE_DIR`/`APR_LIBRARIES` to `${CMAKE_INSTALL_PREFIX}`,
  so a shared prefix chains automatically if APR installs first.
- svn.exe links `libsvn_*-1.dll`; add those dirs plus `<prefix>/bin` to PATH to
  run it. Failure mode is `0xC000007B`, which *looks* like a bitness mismatch
  but here just means a dependent DLL wasn't found.

## 4. Do not run two captures concurrently

Both stacks in this workspace reconfigure the **same** OpenSSL source tree
(`stage3/src/openssl`) with different prefixes. Worse, `cov-build`'s compiler
interception can pick up an unrelated concurrent MSVC build and pollute the
emit, corrupting attribution. Run captures strictly one at a time, or give each
stack its own OpenSSL checkout.

## 5. Coverity models want the opposite build

If the goal is writing Coverity models for dependencies (to find defects in the
*application* that taint-analysis misses at the library boundary), note that the
shipped model library (`<coverity_root>/library/`) covers only libc, Win32, C++
stdlib, concurrency and posix/sql-injection/win32 — **no third-party OSS
models**, so zlib/OpenSSL/curl/expat/pcre2 are all greenfield.

But a model stands in for code the analysis cannot see. Clean-building every
dependency from source — exactly what this skill tells you to do — puts those
bodies *in* the emit and makes the models moot. Keep two configs against the
same checkouts:

- **capture config** — full source build of all deps → rich monitored SBOM
- **models config** — app only, linked against the already-installed prefix →
  small emit, opaque dep boundary, models do the work

## Worked examples

Complete working scripts ship in `examples/` next to this file. They are the
exact scripts that built both stacks, with every fix above already applied — the
fastest start is to copy one and edit the paths.

| | git 2.51.0 | Subversion 1.14.5 |
|---|---|---|
| Full stack | `examples/git-build_stack.bat` | `examples/svn-build_stack.bat` |
| Resume/iterate | `examples/git-build_git_only.bat` | `examples/svn-build_svn_resume.bat` (from apr-util), `examples/svn-build_svn_only.bat` (svn only) |
| Capture config | `examples/git-config.yaml` | `examples/svn-config.yaml` |
| Capture wrapper | `examples/capture.bat` (reads the token into `BLACKDUCK_API_TOKEN`; identical for both, only `cd` differs) | |
| Install prefix | `<workspace>/prefix` | `<workspace>/prefix-svn` |
| Built | git, zlib, OpenSSL, curl, expat, PCRE2, win-iconv | subversion, apr, apr-util, zlib, OpenSSL, expat, SQLite, lz4, utf8proc |
| BD project | `repo-mon-git@2.51.0` | `repo-mon-svn@1.14.5` |

Measured git result — 1,953 TUs (0 emit-failed; 1951 C / 2 C++) and 258 link
units (79 executables, 96 static libs, 81 object files, 2 shared libs).

- **Black Duck BOM: 6** — Git 2.51.0, OpenSSL 3.6.3, curl 8.21.0, PCRE2 10.45,
  libexpat 2.7.1, zlib 1.3.1. All versions exact, five with GitHub origins
  usable for commit replay (`git/git:v2.51.0`, `madler/zlib:v1.3.1`, …).
- **Monitor: 7 components, 7 monitored, 0 reference-only** — the goal state, and
  the payoff for clean-building everything from source. (Compare
  `repo-mon-mbedtls` 1/3 monitored, `SF90-FW` 0/5.)

Emit primaries per component tree, useful as a shape check on your own capture:
openssl 1192, git 445, curl 238, zlib 34, pcre2 32, libexpat 5, win-iconv 4.
A component contributing only 3-5 primaries is at real risk of not matching.

**The capture alone will NOT put the project in the monitor.** `blackduck-c-cpp`
only creates the Black Duck project; registering with the monitor is a separate
ingest, and skipping it is the most common point of confusion:

```bash
python provisioning/ingest.py --project <name> --version <ver> \
    --emit <output_dir>/cov_emit_links.json --monitor-url http://127.0.0.1:8378
```

Ingest returns immediately and the server recreates asynchronously — poll
`GET /api/db-projects` for `add_status`, and consider it done only when
`GET /api/projects` lists it. See `run-analysis` steps 4-6.

**These scripts hardcode absolute paths** for this machine and workspace —
`C:\Data\repo-monitoring-workspace`, `C:\Coverity\cov-analysis-win64-<ver>`,
`C:\Strawberry` (perl + pkg-config), `C:\Users\<user>\AppData\Local\bin\NASM`
(OpenSSL), VS 2022 Community's `vcvars64.bat`, and Git for Windows' `tclsh`.
Fix those first; they are the top of each script.

Source layout the scripts assume (clone at the exact tag, `.git` intact — see
`run-analysis` for why provenance matters):

- `<workspace>/src/` — git, libexpat, pcre2, win-iconv, subversion, apr,
  apr-util, sqlite, (lz4, utf8proc cloned for VCS reference only; SVN builds
  its own vendored copies)
- `<workspace>/stage3/src/` — zlib, openssl, curl (shared by both stacks)

Verify the stack really linked, rather than just building — a dep can be found,
compiled and still not wired in:

- PCRE2: `git grep -P <pattern>` returns matches
- curl+OpenSSL: `git ls-remote https://...` reaching *certificate verification*
  proves the TLS path (a CA-bundle error is success for this purpose)
- SVN vendored deps: `lz4.obj`, `utf8proc.obj`, `sqlite3wrapper.obj` exist under
  `Release/obj/subversion/libsvn_subr/`
