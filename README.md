# Keydra

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

A web-based management console for key-value servers — **Redis**, **Valkey**, **KeyDB**,
**Dragonfly**, **Garnet**, **Aerospike** and **TiKV**. Multi-user and deployable, rather than
a desktop tool on one person's laptop.

The first five speak RESP, which is the only thing they have in common and the only thing
Keydra needs of them. Aerospike and TiKV speak nothing of the sort and have an engine each.
What a target can do is asked rather than assumed: an Aerospike target arrives with three
tabs and a TiKV target with one, where a Redis target has eleven — because that is what those
stores are.

This repository is the whole of Keydra: the three projects as submodules, and the things that
belong to none of them on their own — the image, the manifests, the tools and the design
documents.

| | |
|---|---|
| [`keydra-backend/`](https://github.com/keydrahq/keydra-backend) | Quarkus, Java 21, non-blocking end to end |
| [`keydra-frontend/`](https://github.com/keydrahq/keydra-frontend) | React and PatternFly, English and Turkish |
| [`keydra-doc/`](https://github.com/keydrahq/keydra-doc) | the manual, modular AsciiDoc, both languages |

Each is developed and released on its own; this one holds them together.

## Clone it

```bash
git clone --recurse-submodules https://github.com/keydrahq/keydra.git
cd keydra
```

If you already cloned without that:

```bash
git submodule update --init --recursive
```

Each directory is named after the repository it is a checkout of, and the layout that
produces — `keydra-backend/`, `keydra-frontend/` and `keydra-doc/` side by side — is the one
the documentation build looks for, so `make docs` inside `keydra-doc/` needs no
configuration.

### What the submodule pointers mean

A submodule pins a **commit**, not a branch. What is checked out here is the set of three
commits that were known to work together, which is deliberate: this is where "0.0.1 meant
these three" is written down.

It is therefore usually behind. To see what each project has now:

```bash
git submodule update --remote        # move each to the tip of its main
```

Do not read a stale pointer as an outdated project. Read it as a version.

## Running the whole thing

```bash
# Every store Keydra speaks to, plus its own PostgreSQL and ClickHouse.
podman play kube deploy/keydra-dev.yaml
#    redis      -> localhost:6479    (target, RESP)
#    valkey     -> localhost:6480    (target, RESP)
#    keydb      -> localhost:6482    (target, RESP)
#    dragonfly  -> localhost:6483    (target, RESP)
#    garnet     -> localhost:6484    (target, RESP)
#    aerospike  -> localhost:3199    (target, its own engine)
#    tikv       -> localhost:20160   (target, its own engine; pd on 2379)
#    postgres   -> localhost:5442    (Keydra's own database)
#    store      -> localhost:6481    (Keydra's shared store, for more than one instance)
#    clickhouse -> localhost:8223    (where readings are kept, when they are)

cd keydra-backend  && ./mvnw quarkus:dev     # http://localhost:8181
cd keydra-frontend && nvm use && yarn dev    # http://localhost:9000
```

The host ports are shifted off the defaults so the pod starts on a machine that already runs
a Redis or a PostgreSQL; inside the pod they keep their canonical numbers. Tear it down with
`podman play kube --down deploy/keydra-dev.yaml`.

## As one image

```bash
podman build --ulimit nofile=16384:16384 -t localhost/keydra:dev -f Containerfile .
podman play kube deploy/keydra-prod.yaml
#    http://localhost:8181
```

The frontend is built into the backend's static resources, so one container serves both and
nothing has to be told where the API lives. What ships is Red Hat's `ubi9/openjdk-21-runtime`,
which is the one image still running a month later and so the one whose errata feed is a real
question; the frontend is built on `node:24-alpine` only because UBI 9 stops at Node 22 and
this project pins 24. The reasoning for each of the three stages is written in the file. **Read the comments in
`deploy/keydra-prod.yaml` before running it anywhere real** — the four settings a deployment
has to decide are in there, each with what it costs to leave alone.

`scripts/smoke-image.sh` builds the image and starts it, which is the check neither project's
test suite can stand in for: what lives between the manifest and the container runtime.

If you would rather not build one, it is published to
[quay.io/keydrahq/keydra](https://quay.io/repository/keydrahq/keydra). `:0.0.1` and `:latest`
come from a tag on this repository and `:main` from a merge into it — a tag here is what a
version of Keydra means, because a version is three commits rather than one and this is the
only place those three are written down. Every published image was started before it was
pushed; the run that pushed it says which three commits it is.

## What is in here

| | |
|---|---|
| `deploy/` | Kubernetes Pod manifests — development and production. Not Compose files |
| `Containerfile` | the single image: frontend built into the backend's resources |



## Documentation

The manual is published from its own repository and is written in English and Turkish, with
screenshots in the language of the page they sit on.

## Licence

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE). Each submodule carries the
same licence.
