"""Deduplicate jobs across sources (spec 8.2).

1. Exact: fingerprint = hash(normalised company + title + city/work-mode).
2. Fuzzy: same normalised company (≥90 similar) and near-identical title (≥92), same
   place. The survivor is the record with the best apply link: direct ATS > company
   site > aggregator redirect; ties go to the longer description.
"""

from __future__ import annotations

from rapidfuzz import fuzz

from jobpilot.matching.normalize import LINK_PRIORITY, JobPosting, norm_company, norm_title


def _better(a: JobPosting, b: JobPosting) -> JobPosting:
    ka = (LINK_PRIORITY[a.link_kind], a.salary_min is not None, len(a.description))
    kb = (LINK_PRIORITY[b.link_kind], b.salary_min is not None, len(b.description))
    return a if ka >= kb else b


def _place(job: JobPosting) -> str:
    return (job.city or job.remote_type or "").lower()


def dedupe(jobs: list[JobPosting]) -> list[JobPosting]:
    by_fp: dict[str, JobPosting] = {}
    for job in jobs:
        fp = job.fingerprint
        by_fp[fp] = _better(by_fp[fp], job) if fp in by_fp else job

    kept: list[JobPosting] = []
    keys: list[tuple[str, str, str]] = []
    for job in by_fp.values():
        company, title, place = norm_company(job.company), norm_title(job.title), _place(job)
        match = None
        for i, (c, t, p) in enumerate(keys):
            if (
                (p == place or not p or not place)
                and fuzz.ratio(c, company) >= 90
                and (fuzz.token_sort_ratio(t, title) >= 92)
            ):
                match = i
                break
        if match is None:
            kept.append(job)
            keys.append((company, title, place))
        else:
            kept[match] = _better(kept[match], job)
    return kept
