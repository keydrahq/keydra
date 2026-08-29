# Security policy

Keydra is four repositories and one product. This one holds no application code: the
manifests, the image definition, the tools and the design documents.

## Report it against the backend

**Do not open a public issue, and do not report it here.**

*Security* → *Report a vulnerability* on
[keydra-backend](https://github.com/keydrahq/keydra-backend/security/advisories/new). That is
where the code that holds credentials lives, and its
[SECURITY.md](https://github.com/keydrahq/keydra-backend/blob/main/SECURITY.md) says what is
in scope. If the fault is in the browser, report it against
[keydra-frontend](https://github.com/keydrahq/keydra-frontend/security/advisories/new)
instead; if you are not sure which, pick either and say so.

## What would be a fault in *this* repository

Narrow, and worth stating because it is easy to miss:

- **A manifest that is insecure by default.** `deploy/keydra-prod.yaml` is what somebody will
  copy. A default that exposes an instance, skips TLS, trusts every proxy or leaves
  enforcement off without saying what that costs is a fault here even though no code changed.
- **A secret in a manifest or a script.** A password, a key or a token committed as a literal
  rather than read from a secret.
- **A script that does something worse than it says.** `scripts/` runs on somebody's machine
  against somebody's server.
- **An image that ships something it should not** — a credential baked into a layer, a
  toolchain in the runtime stage, a process running as root.

## What to expect

An acknowledgement within three working days and an assessment within ten.
