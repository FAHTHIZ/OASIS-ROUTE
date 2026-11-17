"""Fail if any installed runtime dependency has a license incompatible with Apache-2.0 distribution.

Usage: uv run --no-dev --all-extras python scripts/check_licenses.py
Reads License-Expression / License / classifiers from installed distribution metadata. Strong copyleft
(GPL, AGPL, SSPL) and unknown licenses fail; LGPL/MPL are allowed because dependencies are used
unmodified as separate libraries.
"""

from __future__ import annotations

import re
import sys
from importlib import metadata

DENY = re.compile(
    r"\b(A?GPL|GNU (Affero )?General Public|SSPL|Server Side Public|BUSL|Commons Clause)\b", re.I
)
LESSER = re.compile(r"\b(LGPL|Lesser General Public|Library General Public)\b", re.I)
ALLOW = re.compile(
    r"\b(MIT|BSD|Apache|ISC|PSF|Python Software Foundation|MPL|Mozilla|LGPL|Lesser|Unlicense|CC0|Zlib|"
    r"HPND|0BSD|Public Domain)\b",
    re.I,
)
SELF = {"oasis-route"}
# Packages whose metadata omits a license but whose upstream license was verified manually.
VERIFIED: dict[str, str] = {}


def licenses(dist: metadata.Distribution) -> list[str]:
    md = dist.metadata
    out = []
    if expr := md.get("License-Expression"):
        out.append(expr)
    lic = md.get("License") or ""
    if lic and len(lic) < 200 and lic.upper() != "UNKNOWN":
        out.append(lic)
    elif lic:  # full license text pasted into the field: use its first line
        out.append(lic.strip().splitlines()[0])
    out += [c.split("::")[-1].strip() for c in md.get_all("Classifier") or [] if c.startswith("License ::")]
    return [x for x in out if x and x != "OSI Approved"]


def main() -> int:
    bad, report = [], []
    for dist in sorted(metadata.distributions(), key=lambda d: (d.metadata["Name"] or "").lower()):
        name = (dist.metadata["Name"] or "").lower()
        if name in SELF:
            continue
        lic = licenses(dist) or ([VERIFIED[name]] if name in VERIFIED else [])
        joined = "; ".join(dict.fromkeys(lic))
        report.append(f"{name}=={dist.version}: {joined or 'UNKNOWN'}")
        if not lic:
            bad.append(f"{name}: no license metadata")
        elif DENY.search(joined) and not LESSER.search(joined):
            bad.append(f"{name}: {joined}")
        elif not ALLOW.search(joined):
            bad.append(f"{name}: unrecognised license {joined!r}")
    print("\n".join(report))
    if bad:
        print("\nLicense check failed:\n  " + "\n  ".join(bad), file=sys.stderr)
        return 1
    print(f"\nOK: {len(report)} distributions checked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
