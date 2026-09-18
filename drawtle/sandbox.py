"""What isolation this run actually has, reported rather than assumed.

The honest state of affairs
---------------------------
`docker/sandbox.md` specifies an envelope we intend to run in. A specification
is not an implementation, and this machine does not currently have Docker
installed, so **every run so far has executed directly on the host**. That is a
real property of every result in `results/` and it belongs in the provenance
record, not in a footnote.

What this module is for
-----------------------
Any claim of the form "the model cannot touch the filesystem" must be checkable.
`describe()` probes the environment and reports what it finds, and
`provenance()` returns the block that goes into a run's status record so a
result carries its own isolation facts.

Design rule
-----------
**Absent evidence degrades to `none`, never to `enforced`.** If Docker is not
running, or `--sandbox` was not passed, the answer is `none`. There is no
inferred middle ground, because a benchmark that over-reports its own isolation
is worse than one that admits it has none.
"""
import os
import shutil
import subprocess

LEVEL_NONE = "none"            # ran as a normal process on the host
LEVEL_DOCKER = "docker"        # ran inside the container in docker/
LEVEL_DOCKER_UNAVAILABLE = "docker-requested-unavailable"

#: Env var the runner sets when it is executed inside the container, so a run
#: can record that it really was contained rather than trusting the CLI flag.
MARKER = "DRAWTLE_IN_SANDBOX"


def in_container():
    """True when this process is inside the project's container.

    Checks the marker the Dockerfile sets AND the presence of `/.dockerenv`,
    because a container can be entered in ways that bypass the compose env.
    """
    if os.environ.get(MARKER) == "1":
        return True
    return os.path.exists("/.dockerenv")


def docker_available():
    """Whether a usable Docker CLI and daemon are present."""
    exe = shutil.which("docker")
    if not exe:
        return False, "docker CLI not on PATH"
    try:
        p = subprocess.run([exe, "info", "--format", "{{.ServerVersion}}"],
                           capture_output=True, text=True, timeout=8)
    except Exception as exc:
        return False, f"docker info failed: {type(exc).__name__}"
    if p.returncode != 0:
        err = (p.stderr or "").strip().splitlines()
        return False, f"daemon unreachable: {err[-1] if err else 'unknown'}"
    return True, p.stdout.strip()


def describe():
    """Probe and report. Never raises; an unknown is an unknown."""
    contained = in_container()
    ok, detail = docker_available()
    if contained:
        level = LEVEL_DOCKER
        note = "running inside the container; the model has no host filesystem"
    elif ok:
        level = LEVEL_NONE
        note = ("running on the host. Docker is available "
                f"({detail}); use docker/docker-compose.yml to contain a run")
    else:
        level = LEVEL_NONE
        note = (f"running on the host and no container is available "
                f"({detail}). This is the state of every committed result.")
    return {
        "level": level,
        "in_container": contained,
        "docker_available": ok,
        "docker_detail": detail,
        "network_isolated": contained,
        "filesystem_isolated": contained,
        "note": note,
        "spec": "docker/sandbox.md",
    }


def provenance():
    """The block recorded into a run's status so the result carries its own facts."""
    d = describe()
    return {
        "sandbox": d["level"],
        "sandbox_in_container": d["in_container"],
        "sandbox_note": d["note"],
    }


def assert_safe_for_external_models():
    """Guard for the case that matters: a paid API key plus no isolation.

    Refusing outright would be wrong -- the model only ever receives a rendered
    image and returns text, and the harness never executes model output, so an
    unisolated run is not actually dangerous for this bench. But the operator
    should be told once, plainly, rather than assuming a Dockerfile in the repo
    means a container is in use.
    """
    d = describe()
    if d["in_container"]:
        return None
    return ("not running in a container; the harness will execute on the host. "
            "This is safe for this bench -- the model only sees a rendered "
            "image and returns JSON, and its output is never executed -- but no "
            "filesystem or network isolation is in force.")
