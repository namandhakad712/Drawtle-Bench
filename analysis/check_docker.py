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

    # The rasteriser is optional for the mock backend but load-bearing for any
    # real vision backend. Warn once rather than fail, because a text-only or
    # mock-only run does not need it.
    if "cairosvg" not in text:
        print("  WARN: the image does not install cairosvg; real vision runs "
              "inside it will have no frames to send.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
