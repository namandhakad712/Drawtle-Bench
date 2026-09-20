"""Static consistency check between bench.py's imports and the Docker image.

WHY THIS EXISTS
---------------
The image in `docker/` is the OS-isolation envelope. It has never been built on
every development machine, which means a Dockerfile that forgot to `COPY` a
package -- or a package that was added to the repo after the Dockerfile was
written -- produces a build that succeeds and a container that dies on import,
at run time, after the operator has already started paying for a run.

That is a defect no unit test can catch, because the unit tests run from the
source tree where every package is present. This check compares the two
directories against each other instead: every top-level module and package that
`bench.py` imports must be either COPYed into the image or provided by the base
image.

It is deliberately narrow. It does not try to resolve the import graph; it
answers one question -- "can `python bench.py` start inside this image?" -- and
answers it from the source of truth (the Dockerfile) rather than from a copy of
it. Exits non-zero on any missing path so it can gate CI.

Run:  python analysis/check_docker.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DOCKERFILE = os.path.join(ROOT, "docker", "Dockerfile")

# Modules the base image (python:3.x-slim) already provides, so the image does
# not need to COPY them. Kept explicit: adding a name here is a claim that the
# image does not need the repo's version.
STDLIB = {
    "absl", "argparse", "collections", "concurrent", "contextlib", "copy", "csv",
    "dataclasses", "datetime", "decimal", "difflib", "enum", "functools", "glob",
    "gzip", "hashlib", "heapq", "html", "http", "io", "itertools", "json",
    "logging", "math", "multiprocessing", "os", "pathlib", "pickle", "platform",
    "pprint", "queue", "random", "re", "shutil", "signal", "socket", "sqlite3",
    "statistics", "string", "subprocess", "sys", "tempfile", "textwrap",
    "threading", "time", "traceback", "types", "typing", "unicodedata", "unittest",
    "urllib", "uuid", "warnings", "weakref", "xml", "zipfile",
}


# Data files loaded at RUNTIME rather than imported. A missing one does not
# fail at import time -- it fails mid-run, or worse, degrades silently to a
# default that changes the measurement. `providers.json` is the sharpest case:
# without it the bench falls back to four built-in backends and an InternLM run
# would be reported as an unknown backend, which reads as a typo rather than a
# missing file in the image.
REQUIRED_DATA = [
    "drawtle/providers.json",
    "drawtle/model_registry.json",
    "configs/default.json",
]

#: Paths that must NEVER reach the image. Docker has no notion of .gitignore, so
#: `COPY configs/ ./configs/` would otherwise bring `configs/.env` -- live API
#: keys -- into a layer, where they are permanent and readable by anyone who can
#: pull the image. A later `rm` in the same RUN does not undo it: the layer that
#: contained the file still contains it.
#:
#: Checked against .dockerignore, so the guard fails when either the Dockerfile
#: starts copying a new directory or the ignore file stops excluding it.
FORBIDDEN_IN_IMAGE = [
    ".env",
    "configs/.env",
    "credentials.json",
    "overlay.json",
    ".git",
]

#: Modules inside the `web` package that the server imports. `bench.py` only
#: imports `web`, so a module added to the package after the Dockerfile was
#: written would pass the top-level check and fail at run time -- inside the
#: container, after a run had been started.
WEB_SUBMODULES = ["server", "views", "theme", "guard", "supervisor"]


def local_imports(path):
    """Top-level module names imported by a Python file, repo-local only.

    Returns the names as written, so the caller can decide which must exist
    inside the image. Relative imports (`from . import x`) are skipped -- they
    resolve within a package that is already accounted for.
    """
    names = set()
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            # `import a.b` / `from a.b import c` / `from a import b`
            m = re.match(r"^(?:from|import)\s+([A-Za-z_][\w.]*)", line)
            if not m:
                continue
            root = m.group(1).split(".")[0]
            names.add(root)
    return names


def copied_paths(dockerfile_text):
    """The paths a Dockerfile COPYs into the image, normalised.

    Parsed from the file rather than asserted, so the check fails when the
    Dockerfile changes and this list does not.
    """
    out = set()
    for line in dockerfile_text.splitlines():
        m = re.match(r"^\s*COPY\s+(.+?)\s+\S+\s*$", line)
        if not m:
            continue
        for src in m.group(1).split():
            if src.startswith("--"):        # COPY --from=... flags
                continue
            out.add(src.rstrip("/"))
    return out


def _dir_is_copied(rel_path, copied):
    """Is a file inside a COPYed directory, or itself COPYed?

    `COPY drawtle/ ./drawtle/` brings every file under `drawtle/` with it, so a
    check that only compares top-level names would miss nothing here but would
    also be unable to say so. This resolves the question for a full relative
    path.
    """
    if rel_path in copied:
        return True
    top = rel_path.split("/")[0]
    return top in copied


def main():
    if not os.path.exists(DOCKERFILE):
        print(f"FAIL: no Dockerfile at {DOCKERFILE}")
        return 1
    text = open(DOCKERFILE, "r", encoding="utf-8").read()
    copied = copied_paths(text)

    bench = os.path.join(ROOT, "bench.py")
    if not os.path.exists(bench):
        print(f"FAIL: no bench.py at {bench}")
        return 1

    needed = local_imports(bench)
    problems = []
    for name in sorted(needed):
        if name in STDLIB:
            continue
        # A repo-local module must exist on disk to be a real requirement.
        as_file = os.path.join(ROOT, name + ".py")
        as_dir = os.path.join(ROOT, name)
        if not (os.path.exists(as_file) or os.path.isdir(as_dir)):
            # Third-party (or optional). The Dockerfile handles these with pip;
            # a missing one is not this check's business.
            continue
        if os.path.isfile(as_file):
            # A single-file module at the root: the image gets it via the
            # explicit `COPY bench.py ./` style line, so look for the filename.
            if name + ".py" in copied:
                continue
        if name in copied:
            continue
        problems.append(name)

    print("docker image consistency")
    print("  " + os.path.relpath(DOCKERFILE, ROOT))
    print(f"  COPYd paths : {', '.join(sorted(copied)) or '(none)'}")
    print(f"  bench.py needs: {', '.join(sorted(n for n in needed if n not in STDLIB)) or '(stdlib only)'}")
    if problems:
        for p in problems:
            print(f"  FAIL: bench.py imports '{p}', which the image does not COPY.")
            print(f"        Add `COPY {p}/ ./{p}/` to the Dockerfile, or the build "
                  f"succeeds and the container fails at import time.")
        return 1
    print("  PASS: every repo-local module bench.py imports is present in the image.")

    # Runtime data files. These do not fail at import, so nothing else catches
    # them: a missing providers.json degrades to four built-in backends and a
    # missing model_registry.json silently makes every price unknown.
    data_problems = []
    for rel in REQUIRED_DATA:
        if not os.path.exists(os.path.join(ROOT, *rel.split("/"))):
            data_problems.append((rel, "does not exist in the repo"))
        elif not _dir_is_copied(rel, copied):
            data_problems.append((rel, "is not COPYed into the image"))
    if data_problems:
        for rel, why in data_problems:
            print(f"  FAIL: data file {rel} {why}.")
            print("        It is loaded at run time, so the build succeeds and "
                  "the failure appears mid-run.")
        return 1
    print(f"  PASS: all {len(REQUIRED_DATA)} runtime data files are in the image.")

    # A secret in a layer is permanent, so this is a hard failure rather than a
    # warning. The check reads .dockerignore and asks whether each forbidden
    # path is actually excluded from the build context.
    ignore_path = os.path.join(ROOT, ".dockerignore")
    if not os.path.exists(ignore_path):
        print("  FAIL: there is no .dockerignore, so every secret in the repo "
              "reaches the build context.")
        print("        `COPY configs/` would bring configs/.env -- live API keys "
              "-- into an image layer, where they cannot be removed.")
        return 1
    rules = _ignore_rules(open(ignore_path, "r", encoding="utf-8").read())
    leaked = [p for p in FORBIDDEN_IN_IMAGE
              if os.path.exists(os.path.join(ROOT, *p.split("/")))
              and not _is_ignored(p, rules)]
    if leaked:
        for p in leaked:
            print(f"  FAIL: {p} exists in the repo and is NOT excluded by "
                  f".dockerignore.")
            print("        A secret copied into an image layer stays in that "
                  "layer permanently.")
        return 1
    present = [p for p in FORBIDDEN_IN_IMAGE
               if os.path.exists(os.path.join(ROOT, *p.split("/")))]
    print(f"  PASS: {len(present)} secret/local path(s) present in the repo are "
          f"excluded from the build context.")

    # bench.py imports `web`, so the top-level check above passes even if a
    # module inside that package is missing from the image.
    missing = [m for m in WEB_SUBMODULES
               if not os.path.exists(os.path.join(ROOT, "web", m + ".py"))]
    if missing:
        print(f"  FAIL: web/ is missing {', '.join(missing)}, which the server "
              f"imports.")
        return 1
    print(f"  PASS: all {len(WEB_SUBMODULES)} web submodules the server needs "
          f"exist and ship inside web/.")

    # The rasteriser is optional for the mock backend but load-bearing for any
    # real vision backend. Warn once rather than fail, because a text-only or
    # mock-only run does not need it.
    if "cairosvg" not in text:
        print("  WARN: the image does not install cairosvg; real vision runs "
              "inside it will have no frames to send.")

    # Version strings. Two hand-maintained copies had already drifted apart
    # (pyproject said 2.5.0, the dashboard said 2.1.0), and a UI reporting a
    # version the code does not have is worse than reporting none.
    drift = _version_drift()
    if drift:
        print(f"  FAIL: version strings disagree: {drift}")
        print("        drawtle/__init__.py is the source of truth; the others "
              "must match it.")
        return 1
    print(f"  PASS: version is consistent ({drift or _pkg_version()}).")

    # Compose build paths. A `COPY` source is resolved against the SERVICE'S
    # build context, not the repo root -- and Compose resolves `context:` and
    # `dockerfile:` against the compose file's own directory. Two independent
    # prefix bugs were each "fixed" separately and the build still failed,
    # because nobody checked the resolution end to end: `context: .` +
    # `COPY docker/egress_proxy.py` resolves to docker/docker/egress_proxy.py
    # and every `compose build` died with "not found" -- which surfaced to the
    # user as a 502 on the dashboard's Build action.
    comp = _compose_paths()
    if comp:
        for c in comp:
            print(f"  FAIL: docker compose build cannot resolve: {c}")
        print("        A compose file that `config` validates can still fail at "
              "build time; only this path check catches it.")
        return 1
    print("  PASS: every compose service's build context resolves every COPY source.")
    return 0


def _compose_paths():
    """Resolve each compose service's build context; report what does not exist.

    Parses docker/docker-compose.yml with a minimal, strictly-structured reader
    (this file is machine-maintained: services at indent 2, `build:` at indent
    4, `context:`/`dockerfile:` at indent 6). For every service that has a
    build block it then checks, for each COPY source in that service's
    Dockerfile, that the source exists INSIDE the resolved context directory.
    Returns a list of problems (empty when sound).
    """
    compose = os.path.join(ROOT, "docker", "docker-compose.yml")
    if not os.path.exists(compose):
        return ["docker/docker-compose.yml is missing"]
    lines = open(compose, "r", encoding="utf-8").read().splitlines()

    # Indices of indent-2 keys; the build block of a service is the text from
    # its key line up to the next indent-2 key line.
    keys = [i for i, ln in enumerate(lines)
            if re.match(r"^  [^ ].*:$", ln)]
    problems = []
    for a, b in zip(keys, keys[1:] + [len(lines)]):
        block = lines[a:b]
        name = lines[a].strip()[:-1]
        ctx = dkf = None
        for ln in block:
            m = re.match(r"^      (context|dockerfile):\s*(.+)$", ln)
            if m:
                if m.group(1) == "context":
                    ctx = m.group(2).strip()
                else:
                    dkf = m.group(2).strip()
        if ctx is None:          # not a service with a build block
            continue
        cdir = os.path.normpath(os.path.join(ROOT, "docker", ctx))
        dock = os.path.normpath(os.path.join(cdir, dkf or ""))
        if not os.path.isfile(dock):
            problems.append(f"service '{name}': Dockerfile {os.path.relpath(dock, ROOT)} "
                            f"does not exist (context {ctx!r})")
            continue
        for src in copied_paths(open(dock, "r", encoding="utf-8").read()):
            if src.startswith("--"):
                continue
            s = os.path.normpath(os.path.join(cdir, src))
            if not os.path.exists(s):
                problems.append(f"service '{name}': COPY {src!r} resolves to "
                                f"{os.path.relpath(s, ROOT)}, which does not exist "
                                f"in the build context {ctx!r}")
    return problems


def _pkg_version():
    try:
        import tomllib
        with open(os.path.join(ROOT, "pyproject.toml"), "rb") as fh:
            return tomllib.load(fh)["project"]["version"]
    except Exception:
        return None


def _version_drift():
    """Return a description of any disagreement, or None when consistent."""
    py = _pkg_version()
    try:
        sys.path.insert(0, ROOT)
        from drawtle import __version__ as pkg
    except Exception as e:
        return f"could not import drawtle.__version__ ({e})"
    if py != pkg:
        return f"pyproject={py!r} vs drawtle.__version__={pkg!r}"
    # The server must derive from the package, not hardcode its own copy.
    src = open(os.path.join(ROOT, "web", "server.py"), encoding="utf-8").read()
    if 'from drawtle import __version__ as VERSION' not in src:
        return "web/server.py does not read drawtle.__version__"
    return None


def _ignore_rules(text):
    """Parse .dockerignore into (patterns, negations).

    Docker's format is close enough to gitignore's for this purpose: one pattern
    per line, `#` comments, `!` to re-include. Kept deliberately simple -- the
    check needs to answer "is this path excluded?", not to reimplement Docker's
    matcher, and a wrong answer here is caught by the `!` handling below.
    """
    patterns, negations = [], []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("!"):
            negations.append(line[1:].strip().rstrip("/"))
        else:
            patterns.append(line.rstrip("/"))
    return patterns, negations


def _is_ignored(rel_path, rules):
    """Does .dockerignore exclude this repo-relative path?

    Matches on the path itself, on any ancestor directory, and on the basename,
    which covers the three forms actually used in the file (`configs/.env`,
    `.env`, and `**/.env`). A negation that re-includes the path wins.
    """
    patterns, negations = rules
    parts = rel_path.split("/")
    candidates = {rel_path, parts[-1]}
    for i in range(1, len(parts)):
        candidates.add("/".join(parts[:i]))
    for neg in negations:
        if rel_path == neg or parts[-1] == neg or rel_path.endswith("/" + neg):
            return False
    for pat in patterns:
        if pat.startswith("**/"):
            pat = pat[3:]
        if pat in candidates or rel_path == pat:
            return True
        if pat.endswith("/*") and rel_path.startswith(pat[:-2] + "/"):
            return True
    return False


if __name__ == "__main__":
    sys.exit(main())
