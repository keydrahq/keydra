#!/usr/bin/env python3
"""Split the generated GraphQL schema into one file per domain.

Keydra's GraphQL is written in Java and the schema is derived from it, which is the wrong way
round for reviewing an API: nobody reads a diff of annotations and sees that an argument stopped
being optional. The schema is therefore checked in, and SchemaDriftTest fails when it and the
running server disagree.

One file per domain rather than one file, mirroring the packages: a query belongs beside the
types it answers with, and a single generated dump is a file people scroll past.

Which domain something belongs to is worked out from the Java rather than from a list kept here.
A list would be a second place to remember, and the thing it would be forgotten for is exactly
the case this exists to catch — something new appearing in the schema.

Usage, with the dev backend running:

    curl -s http://localhost:8181/graphql/schema.graphql | scripts/split-schema.py
"""

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
JAVA = ROOT / "backend/src/main/java/io/keydra"
OUT = ROOT / "backend/src/main/graphql"

# What each domain's file says it is for. A domain with nothing here still gets a file; it just
# gets a plainer heading.
HEADINGS = {
    "alerts": "Rules, and what they had to say.",
    "admin": "What a target is underneath: its settings, its shape, its accounts, its keyspace.",
    "authz": "Who exists, what they belong to, and what that is worth.",
    "backup": "Where backups go, and what is already there.",
    "common": "Shapes every domain uses.",
    "connections": "The servers Keydra manages, and how each one is doing.",
    "keys": "Keys, and moving them from one server to another.",
    "monitoring": "What a target is doing, and what it did.",
    "schedule": "Work arranged in advance, and what became of it.",
    "security": "What was done, by whom, and whether it was allowed.",
    "tunnels": "The jump hosts Keydra reaches targets through.",
}

# Scalars the runtime declares rather than anything here defining.
BUILT_IN = {"BigInteger", "Date", "DateTime", "Time", "BigDecimal"}

# The three roots. Their fields are distributed to the domains that declare them; the roots
# themselves are never written out, because each domain writes `extend type Query` instead.
ROOTS = ("Query", "Mutation", "Subscription")


def domain_of_path(path: pathlib.Path) -> str:
    """The package directly under io.keydra that a file sits in."""
    return path.relative_to(JAVA).parts[0]


def operation_domains() -> dict[str, str]:
    """Every GraphQL operation name, and the domain whose graphql package declares it."""
    found = {}
    for java in JAVA.glob("*/graphql/*.java"):
        text = java.read_text()
        for name in re.findall(r'@(?:Query|Mutation|Subscription)\("([^"]+)"\)', text):
            found[name] = domain_of_path(java)
    return found


def type_domains() -> dict[str, str]:
    """Every GraphQL type name, and the domain whose Java declares it.

    Two ways a name arrives: an explicit ``@Name("Thing")``, or a Java type whose simple name is
    the schema's name. Input types are the schema's name with ``Input`` appended, and nested
    records are named by their own simple name — both are handled by indexing every declaration.
    """
    found = {}
    for java in JAVA.rglob("*.java"):
        if "/graphql/" in str(java) and java.name.endswith("Queries.java"):
            # A resolver class is not a type; its @Name annotations name operations.
            continue
        domain = domain_of_path(java)
        text = java.read_text()
        for name in re.findall(r'@Name\("([A-Z][A-Za-z0-9]*)"\)', text):
            found.setdefault(name, domain)
        for name in re.findall(r"^\s*(?:public\s+)?(?:static\s+)?(?:sealed\s+)?"
                               r"(?:record|enum|interface|class)\s+([A-Z][A-Za-z0-9]*)",
                               text, re.M):
            found.setdefault(name, domain)
            found.setdefault(name + "Input", domain)
    return found


def blocks_of(schema: str) -> dict[str, str]:
    """The schema as top-level blocks, keyed by the name each declares."""
    found = {}
    name, buffer = None, []
    for line in schema.splitlines():
        header = re.match(r"^(type|enum|input|interface|union|scalar) (\w+)", line)
        if header and name is None:
            name, buffer = header.group(2), [line]
            if "{" not in line:
                found[name] = "\n".join(buffer)
                name = None
            continue
        if name is not None:
            buffer.append(line)
            if line.startswith("}"):
                found[name] = "\n".join(buffer)
                name = None
    return found


def members(block: str) -> list[str]:
    """A block's fields, each with the description line above it, as one string per field."""
    lines = block.splitlines()[1:-1]
    out, held, depth = [], [], 0
    for line in lines:
        held.append(line)
        depth += line.count("(") - line.count(")")
        if depth == 0 and line.strip() and not line.strip().startswith('"'):
            out.append("\n".join(held))
            held = []
    if held:
        out.append("\n".join(held))
    return out


def named(member: str) -> str | None:
    match = re.search(r"^\s{2}(\w+)", member, re.M)
    return match.group(1) if match else None


def main() -> int:
    schema = sys.stdin.read()
    if not schema.strip():
        print("No schema on stdin. Is the backend running?", file=sys.stderr)
        return 1

    blocks = blocks_of(schema)
    by_operation = operation_domains()
    by_type = type_domains()

    # Root fields, grouped by the domain that declares each operation.
    operations: dict[str, dict[str, list[str]]] = {}
    unplaced_operations = []
    for root in ROOTS:
        if root not in blocks:
            continue
        for member in members(blocks[root]):
            name = named(member)
            domain = by_operation.get(name)
            if domain is None:
                unplaced_operations.append(f"{root}.{name}")
                continue
            operations.setdefault(domain, {}).setdefault(root, []).append(member)

    # Types, grouped the same way.
    types: dict[str, list[str]] = {}
    unplaced_types = []
    for name, block in blocks.items():
        if name in ROOTS or name in BUILT_IN:
            continue
        domain = by_type.get(name)
        if domain is None:
            unplaced_types.append(name)
            continue
        types.setdefault(domain, []).append(block)

    for existing in OUT.glob("*.graphql"):
        existing.unlink()

    for domain in sorted(set(operations) | set(types)):
        heading = HEADINGS.get(domain, f"The {domain} domain.")
        parts = [
            f"# {heading}\n#\n"
            f"# Part of Keydra's GraphQL schema, checked in so a change to the API arrives as a\n"
            f"# change to a file somebody reads. Generated from io.keydra.{domain} by\n"
            f"# scripts/split-schema.py; SchemaDriftTest fails when this and the server disagree.\n"
        ]
        for root in ROOTS:
            wanted = operations.get(domain, {}).get(root, [])
            if wanted:
                parts.append("extend type %s {\n%s\n}\n" % (root, "\n".join(wanted)))
        for block in sorted(types.get(domain, [])):
            parts.append(block + "\n")
        (OUT / f"{domain}.graphql").write_text("\n".join(parts))
        print(f"wrote {domain}.graphql")

    if unplaced_types or unplaced_operations:
        # Loud rather than silent: something is in the schema that no package explains, and a file
        # quietly missing it is how a checked-in schema stops being the schema.
        print(f"UNPLACED types: {unplaced_types}", file=sys.stderr)
        print(f"UNPLACED operations: {unplaced_operations}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
