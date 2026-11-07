# Security policy

## The isolation model

**The sandbox is the protocol.** A model under test receives messages and
returns text. It never gets filesystem, shell, or network access — with or
without a container. Docker (docker/) is the *envelope* around that
contract: an unprivileged user, no route outward except the allow-list
egress proxy, and everything the bench writes confined to two mounted
volumes. Runs record the isolation they actually had (probed, not assumed)
in every status sidecar.

## Keys

- Provider keys are read from the environment first, then
  `configs/local/keys.json` (created 0600, **gitignored**; check
  `.gitignore` — `configs/local/` is never committed).
- Keys are masked in every view and never logged.
- If a key was ever printed to a terminal or pasted anywhere, rotate it at
  the provider. `drawtle key clear BACKEND` then re-set.

## The control centre

- Binds `127.0.0.1` by default. It has a launch button; it is not for the
  public internet.
- To expose it beyond localhost, set `DRAWTLE_BOARD_TOKEN=<long random>`
  and send it as `X-Board-Token`. Without a token the listener refuses.
- There is no docker control endpoint (report only) and no shell-out from
  any handler. All API inputs are validated; the client renders everything
  through Preact's escaping — no `innerHTML`, no string-built HTML on the
  client.
- The response cache stores model payloads under
  `results/response-cache/` (gitignored). It contains model outputs, not
  keys; treat your results tree accordingly when sharing.

## Reporting a vulnerability

Open a private security advisory (GitHub → Security → Advisories) or
contact the maintainer directly. Please include reproduction steps and the
affected commit; the parity fixtures make regressions easy to pinpoint.

## What this bench deliberately does not do

- No model output is ever executed, parsed as code, or followed as a URL.
- No auto-fetching of models, prices or endpoints at run time beyond the
  declared registry; discovery is an explicit, logged command
  (`drawtle models --backend B`).
- No telemetry. Nothing leaves the machine except the model calls you
  initiated, through the egress you configured.
