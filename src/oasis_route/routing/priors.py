"""Name-based capability priors for models that have no quality evidence.

Real-provider models arrive from ``/models`` listings without any quality data, and Oasis Route never
invents benchmark scores. Without *some* ordering, however, ``auto`` would rank an unrated 1B model and an
unrated 400B model purely by latency, which defeats "strongest first" for zero-ops users.

This module derives a coarse, deterministic **prior** from the model identifier only (parameter count,
size/tier words, generation number, task-specialist markers). It is deliberately conservative:

* A prior is **never** treated as evidence. Operator-supplied quality evidence always ranks first; priors
  only order unrated models *among themselves* (see ``router.rank_key``).
* Priors do not satisfy ``min_quality`` and never mark a candidate as rated.
* The output is a 0-100 number on the same scale as quality evidence purely for readability; it carries
  ``PRIOR_SOURCE`` so every route explanation shows that it is a heuristic.

Operators who want precise ordering should attach evidence with ``PATCH /admin/models/{id}``.
"""

from __future__ import annotations

import math
import re

from oasis_route.domain.enums import Task

PRIOR_SOURCE = "heuristic:model-name prior (not a benchmark)"

# Parameter counts such as "70b", "8x7b", "1.5b", "405B". Active-parameter suffixes ("a22b") are skipped.
_PARAMS = re.compile(r"(?<![a-z0-9])(?:(\d+)x)?(\d+(?:\.\d+)?)b(?![a-z0-9])", re.I)
_ACTIVE = re.compile(r"(?<![a-z0-9])a\d+(?:\.\d+)?b(?![a-z0-9])", re.I)
# Generation / version number directly after a family token, e.g. "gemini-2.5", "llama-3.3", "gpt-4o".
_VERSION = re.compile(r"(?:^|[/_-])[a-z]+[-_]?v?(\d+(?:\.\d+)?)", re.I)

# Tier words, strongest first. First match wins inside each group.
_TIER_UP = (
    (re.compile(r"\b(opus|ultra|max)\b|-max\b", re.I), 14.0),
    (re.compile(r"(?<![a-z])(pro|large|sonnet|reasoner|thinking)(?![a-z])", re.I), 10.0),
    (re.compile(r"(?<![a-z0-9])(o1|o3|o4)(?![a-z0-9])", re.I), 10.0),
    (re.compile(r"(?<![a-z])(medium|plus|turbo|instruct)(?![a-z])", re.I), 2.0),
)
_TIER_DOWN = (
    (re.compile(r"(?<![a-z])(nano|tiny|micro)(?![a-z])", re.I), -18.0),
    (re.compile(r"(?<![a-z])(lite|mini|small|haiku|instant|8k)(?![a-z])", re.I), -10.0),
    (re.compile(r"(?<![a-z])(flash)(?![a-z])", re.I), -4.0),
)
_CODING = re.compile(r"(coder|codestral|devstral|code(?![a-z]))", re.I)
_REASONING = re.compile(r"(reason|thinking|(?<![a-z])r1(?![a-z0-9])|(?<![a-z])o[134](?![a-z0-9])|qwq)", re.I)
_VISION = re.compile(r"(vision|-vl\b|vl-|pixtral|llava)", re.I)
_DEMOTE = re.compile(r"(preview-?\d{2}-\d{2}|exp(?:erimental)?(?![a-z])|distill|base(?![a-z]))", re.I)

# Hosted models that publish no size in their identifier are usually provider flagships or their tiered
# variants (tier words below adjust them). 62 sits between a ~70B and a ~120B open-weight model.
BASELINE = 62.0

# Widely published total parameter counts for open-weight families whose IDs omit the size.
_KNOWN_SIZES: tuple[tuple[re.Pattern[str], float], ...] = (
    (re.compile(r"deepseek-(r1|v3|chat|reasoner)(?![-.]?distill)", re.I), 671.0),
    (re.compile(r"kimi-k2", re.I), 1000.0),
    (re.compile(r"glm-4\.[5-9](?!-air)", re.I), 355.0),
    (re.compile(r"glm-4\.[5-9]-air", re.I), 106.0),
)


def param_billions(upstream_id: str) -> float | None:
    """Total parameter count in billions parsed from the identifier (or a known family), if present."""
    for rx, size in _KNOWN_SIZES:
        if rx.search(upstream_id):
            return size
    text = _ACTIVE.sub(" ", upstream_id)
    best: float | None = None
    for experts, size in _PARAMS.findall(text):
        total = float(size) * (int(experts) if experts else 1)
        best = total if best is None else max(best, total)
    return best


def _generation(name: str) -> float:
    """Small bonus for newer generations within a family ("llama-3.3" > "llama-3.1"); dates are ignored."""
    for m in _VERSION.finditer(name):
        nxt = name[m.end() : m.end() + 1]
        if nxt in ("b", "x"):  # "8x7b", "70b": a size, not a generation
            continue
        v = float(m.group(1))
        if v >= 20:  # "2507", "20240620": release dates
            continue
        return min(v, 5.0)
    return 0.0


def model_prior(upstream_id: str, task: Task = Task.GENERAL) -> float:
    """Return a deterministic 0-100 heuristic prior for ordering unrated models."""
    name = upstream_id.lower().rsplit("/", 1)[-1] if "/" in upstream_id else upstream_id.lower()
    score = BASELINE
    params = param_billions(name)
    if params is not None and params > 0:
        # 1B -> 30, 8B -> ~46, 70B -> ~62, 235B -> ~71, 1T -> ~82. Capability grows sub-linearly with size.
        score = 30.0 + 5.25 * math.log2(max(params, 0.1))
    for rx, delta in _TIER_UP:
        if rx.search(name):
            score += delta
            break
    for rx, delta in _TIER_DOWN:
        if rx.search(name):
            score += delta
            break
    score += _generation(name)
    if _DEMOTE.search(name):
        score -= 3.0
    specialist = {Task.CODING: _CODING, Task.REASONING: _REASONING, Task.VISION: _VISION}.get(task)
    if specialist is not None and specialist.search(name):
        score += 8.0
    return round(max(0.0, min(100.0, score)), 2)
