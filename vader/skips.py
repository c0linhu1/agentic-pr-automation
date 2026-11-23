"""Which files a review leaves out, and whether each skip is safe: a PR that skips a file that could hide
hand-written or tampered code isn't auto-merged. Also the paths that always need a person."""
import re
from pathlib import PurePosixPath
from typing import Callable

from . import config
from .diffs import FileDiff

# Each lockfile, and the manifests whose change explains it (exact names, or suffixes starting with ".").
_NPM = ("package.json",)
_PY = ("pyproject.toml",)
_DOTNET = (".csproj", ".fsproj", ".vbproj", "directory.packages.props", "directory.build.props")
_GRADLE = ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts", "libs.versions.toml")
LOCKFILE_MANIFESTS = {
    "package-lock.json": _NPM, "npm-shrinkwrap.json": _NPM, "yarn.lock": _NPM, "pnpm-lock.yaml": _NPM,
    "bun.lock": _NPM, "bun.lockb": _NPM, "deno.lock": ("deno.json", "deno.jsonc", "package.json"),
    "packages.lock.json": _DOTNET, "poetry.lock": _PY, "uv.lock": _PY, "pdm.lock": _PY,
    "pipfile.lock": ("pipfile",), "cargo.lock": ("cargo.toml",), "composer.lock": ("composer.json",),
    "gemfile.lock": ("gemfile", ".gemspec"), "go.sum": ("go.mod",), "gradle.lockfile": _GRADLE,
    "package.resolved": ("package.swift", ".pbxproj"), "podfile.lock": ("podfile",),
    "pubspec.lock": ("pubspec.yaml",), "mix.lock": ("mix.exs",), "flake.lock": ("flake.nix",),
}
LOCKFILES = set(LOCKFILE_MANIFESTS)
# Package registries. Any other place in a lockfile's download lines could be a swapped tarball, so that
# lockfile stops an auto-merge. GitHub isn't one: anyone can host a fork or tarball there, which is how a
# tampered lockfile usually points at its package. Add a private registry (or GitHub, knowing what that
# allows) with TRUSTED_PACKAGE_HOSTS.
PACKAGE_HOSTS = {"registry.npmjs.org", "registry.yarnpkg.com", "pypi.org", "files.pythonhosted.org",
                 "pypi.python.org", "crates.io", "index.crates.io", "static.crates.io", "rubygems.org",
                 "api.nuget.org", "pub.dev", "pub.dartlang.org", "repo.maven.apache.org", "repo1.maven.org",
                 "plugins.gradle.org", "cdn.cocoapods.org", "repo.hex.pm", "hex.pm", "proxy.golang.org", "jsr.io",
                 "deno.land"}
TRUSTED_URLS = ("github.com/rust-lang/crates.io-index",)  # Cargo's registry index, on GitHub
# A download line: a key that names where a package comes from, then a URL (group 1: host and path).
# "url" means that only in some lockfiles; in npm ones it's also used for funding links.
_DOWNLOAD_KEYS = r"(?:\b(?:resolved|resolution|tarball|registry|source|remote|location)\b|:git\b)"
_URL = r'[a-z+]*://(?:[^@/\s"\']+@)?([a-z0-9.-]+[^\s"\'}\],]*)'
DOWNLOAD_LINE = re.compile(rf"^\+.*?{_DOWNLOAD_KEYS}.{{0,80}}?{_URL}", re.M | re.I)
DOWNLOAD_LINE_WITH_URL = re.compile(rf"^\+.*?(?:{_DOWNLOAD_KEYS}|\burl\b).{{0,80}}?{_URL}", re.M | re.I)
URL_KEY_LOCKFILES = {"poetry.lock", "uv.lock", "pdm.lock", "pubspec.lock", "pipfile.lock", "flake.lock"}
# Formats whose download lines can't be told apart line by line (Composer mixes them with support and
# funding links), or can't be read at all.
UNCHECKABLE_LOCKFILES = {"composer.lock", "bun.lockb"}
GENERATED_SUFFIXES = (".designer.cs", ".g.cs", ".g.i.cs", "modelsnapshot.cs", ".generated.cs",  # .NET
                      ".pb.go", "_pb2.py", "_pb2_grpc.py", "_pb2.pyi", ".pb.cc", ".pb.h", ".pb.swift",  # protobuf
                      ".pb.dart", "_pb.js", "_pb.d.ts", "_grpc_pb.js", ".g.dart", ".freezed.dart")
PROTOBUF_SUFFIXES = tuple(s for s in GENERATED_SUFFIXES if "pb" in s)
MINIFIED_SUFFIXES = (".min.js", ".min.css")
# Folders that only ever hold build output or installed packages, at any depth. bin/ counts only as
# bin/Debug or bin/Release (.NET), since elsewhere it holds hand-written scripts.
BUILD_DIRS = {"obj", "node_modules", ".vs", ".next", ".nuxt", ".svelte-kit", "__pycache__", ".gradle", ".venv",
              "deriveddata"}
# Names that are also ordinary source folders (src/coverage/ for insurance), so they count only near the
# repo root (or a monorepo project's root), and only for the kinds of file a build writes there. build/
# isn't one: when it's committed it usually holds hand-written scripts (Go's build/package, webpack configs).
TOP_BUILD_DIRS = {"dist", "out", "target", "coverage"}
BUILD_OUTPUT_EXT = {".js", ".mjs", ".cjs", ".css", ".html", ".htm", ".json", ".xml", ".info", ".txt"}
SOURCE_ROOTS = {"src", "lib", "app", "pkg", "internal", "source"}  # one level down from these is source
MONOREPO_ROOTS = {"packages", "apps", "services", "libs", "modules", "projects"}  # packages/a/dist is output
TOP_VENDORED_DIRS = {"vendor", "third_party", "third-party", "pods", "bower_components"}
VENDORED_PATHS = ("wwwroot/lib/",)
# Skips that can't hide hand-written or tampered code. "generated" (by file name) is safe only once the file
# is confirmed to start with a generator's banner; any other skip stops an auto-merge. A minified file can
# hold anything (no one reads a new evil.min.js), so it isn't safe; nor is compiled JavaScript, which can
# differ from the TypeScript that was reviewed.
SAFE_SKIPS = {"image, font or document", "source map", "lockfile"}
# Binaries that can't run: safe to skip. Anything that can run or unpack (a DLL, a JAR, a zip), keys and
# certificates, and binaries of a type not listed here need a person.
INERT_EXT = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".bmp", ".webp", ".avif", ".tif", ".tiff", ".pdf", ".woff",
             ".woff2", ".ttf", ".eot", ".otf", ".mp4", ".webm", ".mp3", ".wav", ".xlsx", ".docx", ".pptx"}
EXECUTABLE_EXT = {".zip", ".gz", ".tgz", ".7z", ".rar", ".jar", ".war", ".class", ".pyc", ".so", ".dylib", ".o",
                  ".a", ".lib", ".wasm", ".dll", ".exe", ".msi", ".pdb", ".nupkg", ".whl", ".xlsm", ".docm"}
KEY_EXT = {".snk", ".pfx", ".p12", ".jks", ".keystore"}
TS_SOURCE = (".ts", ".tsx", ".mts", ".cts")
# Only in compiled output: a source-map link, or the CommonJS module marker TypeScript writes.
COMPILED_JS_MARKERS = ("sourcemappingurl=", 'object.defineproperty(exports, "__esmodule"')
# A hunk that starts at line 1, and the first few lines after it: where generators put their banner.
FILE_START = re.compile(r"^@@ -\d+(?:,\d+)? \+1(?:,\d+)? @@.*\n((?:[ +].*\n?){1,5})", re.M)


def skip_reason(f: FileDiff, ts_stems: set[str], names: set[str] = frozenset()) -> str | None:
    """Why this file shouldn't be reviewed, or None to review it. `names`: every file name in the change."""
    path = f.path.replace("\\", "/")
    lower = path.lower()
    p = PurePosixPath(lower)
    if p.name in LOCKFILES:  # before the binary check: a binary lockfile (bun.lockb) can't be checked
        return _lockfile_reason(p.name, f.text, names)
    if p.suffix in KEY_EXT:
        return "key or certificate"
    if p.suffix in EXECUTABLE_EXT:
        return "executable or archive"
    if p.suffix in INERT_EXT:
        return "image, font or document"
    if "Binary files " in f.text[:2000] or "GIT binary patch" in f.text[:2000]:
        return "binary of an unknown type"
    if p.suffix == ".map":
        return "source map"
    if lower.endswith(MINIFIED_SUFFIXES):
        return "minified"
    if lower.endswith(GENERATED_SUFFIXES):
        return "generated"
    if _banner_in_diff(f.text):
        return "marked as generated"
    dirs = p.parts[:-1]
    if dirs[:1] and dirs[0] in SOURCE_ROOTS:
        top = dirs[:1]
    elif dirs[:1] and dirs[0] in MONOREPO_ROOTS:
        top = dirs[:3]
    else:
        top = dirs[:2]
    if (any(d in BUILD_DIRS for d in dirs)
            or any(d == "bin" and nxt in ("debug", "release") for d, nxt in zip(dirs, dirs[1:]))
            or (p.suffix in BUILD_OUTPUT_EXT and any(d in TOP_BUILD_DIRS for d in top))):
        return "build output"
    if any(d in TOP_VENDORED_DIRS for d in top) or any(f"/{v}" in f"/{lower}" for v in VENDORED_PATHS):
        return "vendored library"
    # A .js file is compiled output only with evidence, since a hand-written one can share a .ts file's name.
    if p.suffix in (".js", ".mjs", ".cjs") and p.stem in ts_stems and (
            "wwwroot" in dirs or any(m in f.text.lower() for m in COMPILED_JS_MARKERS)):
        return "compiled from TypeScript"
    return None


def _lockfile_reason(name: str, text: str, names: set[str]) -> str:
    """A lockfile is safe to skip when a manifest in the same change explains it and it only downloads
    from the usual registries; otherwise it could be a tampered dependency and needs a person."""
    manifests = LOCKFILE_MANIFESTS[name]
    if not any(n in manifests or n.endswith(tuple(m for m in manifests if m.startswith("."))) for n in names):
        return f"lockfile changed without {manifests[0]}"
    if name in UNCHECKABLE_LOCKFILES or "Binary files " in text[:2000] or "GIT binary patch" in text[:2000]:
        return "lockfile in a format Vader can't check"
    trusted = PACKAGE_HOSTS | config.TRUSTED_PACKAGE_HOSTS
    lines = DOWNLOAD_LINE_WITH_URL if name in URL_KEY_LOCKFILES else DOWNLOAD_LINE
    odd = set()
    for url in lines.findall(text):
        url = url.lower().rstrip("/")
        host = url.split("/", 1)[0].split(":", 1)[0]
        if not (any(host == t or host.endswith("." + t) for t in trusted) or url in TRUSTED_URLS):
            odd.add(host)
    if odd:
        return f"lockfile downloads from {', '.join(sorted(odd)[:3])}"
    return "lockfile"


def unsafe_skips(skipped: list[dict], generated_ok: Callable[[str], bool] = lambda path: False) -> list[dict]:
    """Skipped files that could hide hand-written or tampered code, so a person should look before a merge.
    A file skipped by its generated-looking name is fine only if `generated_ok(path)` confirms it."""
    return [s for s in skipped if s["reason"] not in SAFE_SKIPS
            and not (s["reason"] == "generated" and generated_ok(s["path"]))]


def generated_source_changed(path: str, reviewed: set[str]) -> bool:
    """A file the generator makes this one from was changed in the change and reviewed: the migration next
    to X.Designer.cs (X.cs), user.dart for user.g.dart, a .proto for protobuf output, a migration in the
    folder for a model snapshot. A new generated file without one could be hand-written under a fake banner."""
    p = PurePosixPath(path.replace("\\", "/").lower())
    sources = [PurePosixPath(r.replace("\\", "/").lower()) for r in reviewed]
    if p.name.endswith("modelsnapshot.cs"):
        return any(s.parent == p.parent for s in sources)
    suffix = next((s for s in GENERATED_SUFFIXES if p.name.endswith(s)), "")
    base = p.name[:-len(suffix)] if suffix else p.stem
    if suffix in PROTOBUF_SUFFIXES:  # user_pb2.py, user_grpc.pb.go, ... from user.proto, in any folder
        return any(s.suffix == ".proto" and (base == s.stem or base.startswith(s.stem + "_")) for s in sources)
    return any(s.parent == p.parent and s.name != p.name and (s.name == base or s.name.startswith(base + "."))
               for s in sources)


def has_generated_banner(head: str) -> bool:
    """These first lines of a file are a generator's do-not-edit banner (Go, .NET, protobuf, Dart, ...)."""
    head = head.lower()
    return ("<auto-generated" in head or "@generated" in head
            or ("generated" in head and any(w in head for w in ("do not edit", "do not modify", "don't edit"))))


def _banner_in_diff(text: str) -> bool:
    """The diff shows the file starting with a generator's banner."""
    m = FILE_START.search(text)
    return bool(m) and has_generated_banner(m.group(1))


def path_pattern(glob: str) -> re.Pattern:
    """A glob as a regex: `**` spans folders, `*` and `?` stay within one, a trailing `/` means everything
    under it, and a name without `/` matches in any folder. Case is ignored, so a rename can't dodge it."""
    p = glob.strip().removeprefix("./").lstrip("/")
    p = p + "**" if p.endswith("/") else p
    p = p if "/" in p else "**/" + p
    out, i = "", 0
    while i < len(p):
        if p.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif p.startswith("**", i):
            out, i = out + ".*", i + 2
        else:
            out, i = out + {"*": "[^/]*", "?": "[^/]"}.get(p[i], re.escape(p[i])), i + 1
    return re.compile(out + r"\Z", re.I)


def needs_person(paths: list[str], globs: list[str]) -> list[str]:
    """The paths that match one of `globs`, in order, each once."""
    patterns = [path_pattern(g) for g in globs]
    return [p for p in dict.fromkeys(paths) if any(r.match(p) for r in patterns)]


def filter_files(files: list[FileDiff]) -> tuple[list[FileDiff], list[dict]]:
    """(files to review, [{"path", "reason"}] for the ones dropped)."""
    ts_stems = {PurePosixPath(f.path.lower()).stem for f in files if f.path.lower().endswith(TS_SOURCE)}
    names = {PurePosixPath(f.path.replace("\\", "/").lower()).name for f in files}
    kept, skipped = [], []
    for f in files:
        reason = skip_reason(f, ts_stems, names)
        if reason:
            skipped.append({"path": f.path, "reason": reason})
        else:
            kept.append(f)
    return kept, skipped
