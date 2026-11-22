from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from jobpilot.resume.extract import (
    TELEGRAM_DOWNLOAD_LIMIT,
    EmptyResumeError,
    FileTooLargeError,
    ScannedPdfError,
    SourceKind,
    UnsupportedFileError,
    check_size,
    decode_text,
    extract_file,
    extract_pasted,
)
from jobpilot.resume.profile import (
    finalize,
    heuristic_extract,
    infer_seniority,
    looks_like_resume,
    merged_months,
    roles_from_skills,
)
from jobpilot.resume.schema import ExperienceItem
from jobpilot.services.profile import apply_simple_correction
from tests import factories

FIX = Path(__file__).parents[1] / "fixtures" / "resumes"
TODAY = date(2026, 11, 21)


def _read(name: str) -> bytes:
    return (FIX / name).read_bytes()


def test_pdf_docx_txt_all_extract() -> None:
    pdf = extract_file("cv.pdf", _read("fresher_backend.pdf"))
    docx = extract_file("cv.docx", _read("mid_data_tables.docx"))
    txt = extract_file("cv.txt", _read("fresher_backend.txt"))
    assert pdf.kind is SourceKind.PDF and "Aarav Mehta" in pdf.text
    assert docx.kind is SourceKind.DOCX and "Flipkart" in docx.text  # text inside a table
    assert txt.kind is SourceKind.TXT and "FastAPI" in txt.text


def test_scanned_pdf_detected() -> None:
    with pytest.raises(ScannedPdfError, match="paste"):
        extract_file("scan.pdf", _read("scanned.pdf"))


@pytest.mark.parametrize("name", ["cv.doc", "photo.jpg", "cv.pages"])
def test_unsupported_types(name: str) -> None:
    with pytest.raises(UnsupportedFileError):
        extract_file(name, b"whatever")


def test_size_limit_is_telegrams_20mb() -> None:
    check_size(TELEGRAM_DOWNLOAD_LIMIT)
    with pytest.raises(FileTooLargeError, match="20 MB"):
        check_size(TELEGRAM_DOWNLOAD_LIMIT + 1)


def test_text_decoding_and_cleanup() -> None:
    assert decode_text("Café résumé".encode("cp1252")) == "Café résumé"
    assert decode_text("﻿Hello".encode()) == "Hello"
    pasted = extract_pasted("Name\r\n\r\n\r\n\r\nSkills: Python\x00 " + "x " * 100)
    assert "\n\n\n" not in pasted.text
    assert "\x00" not in pasted.text
    with pytest.raises(EmptyResumeError):
        extract_pasted("too short")


def test_experience_merges_overlaps_and_separates_internships() -> None:
    items = [
        ExperienceItem(
            title="Dev",
            company="A",
            start="2022-01",
            end="2023-12",
            is_internship=False,
            highlights=[],
        ),
        ExperienceItem(
            title="Freelance",
            company="B",
            start="2023-06",
            end="2024-06",
            is_internship=False,
            highlights=[],
        ),  # overlaps A by 7 months
        ExperienceItem(
            title="Intern",
            company="C",
            start="2021-06",
            end="2021-08",
            is_internship=True,
            highlights=[],
        ),
    ]
    assert merged_months(items, TODAY, internships=False) == 30  # Jan 2022 – Jun 2024
    assert merged_months(items, TODAY, internships=True) == 3


@pytest.mark.parametrize(
    ("years", "student", "title", "expected"),
    [
        (0, True, None, "intern"),
        (0.2, False, None, "fresher"),
        (1.5, False, None, "junior"),
        (3, False, None, "mid"),
        (6, False, None, "senior"),
        (6, False, "Engineering Lead", "lead"),
        (10, False, None, "lead"),
    ],
)
def test_seniority_rules(years: float, student: bool, title: str | None, expected: str) -> None:
    assert infer_seniority(years, student, 0, title) == expected


def test_rule_parser_on_fixtures() -> None:
    text = extract_file("cv.txt", _read("fresher_backend.txt")).text
    p = finalize(heuristic_extract(text, TODAY), text, today=TODAY, parsed_with_ai=False)
    assert p.full_name == "Aarav Mehta"
    assert p.location_current == "Hyderabad"
    assert p.email == "aarav.mehta@example.com"
    assert p.is_student and p.seniority == "intern"
    assert p.internship_months == 3
    assert {"Python", "FastAPI", "PostgreSQL"} <= set(p.skills.all())
    assert "Backend Developer" in p.inferred_target_roles
    assert not p.parsed_with_ai

    docx = extract_file("cv.docx", _read("mid_data_tables.docx")).text
    q = finalize(heuristic_extract(docx, TODAY), docx, today=TODAY)
    assert q.location_current == "Bengaluru"
    assert q.seniority == "mid"
    assert q.total_experience_years == pytest.approx(4.9)
    assert q.inferred_target_roles[0] == "Data Analyst"


def test_injected_claims_cannot_change_computed_level() -> None:
    """'Staff Engineer with 20 years' in the text doesn't move seniority: dates decide."""
    text = _read("injection.txt").decode()
    p = finalize(heuristic_extract(text, TODAY), text, today=TODAY)
    assert p.total_experience_years == 0  # only a trainee role
    assert p.seniority in {"intern", "fresher"}


def test_unevidenced_skills_are_dropped() -> None:
    text = "Python developer. Built APIs with FastAPI. Education: B.Tech. Experience: 2024"
    extraction = heuristic_extract(text, TODAY)
    extraction.skills.cloud.append("AWS")  # a hallucinated skill
    extraction.skills.frameworks.append("fastapi")  # alias of a real one
    p = finalize(extraction, text, today=TODAY)
    assert "AWS" not in p.skills.all()
    assert p.skills.all().count("FastAPI") == 1


def test_resume_detector() -> None:
    assert looks_like_resume(_read("fresher_backend.txt").decode())
    assert not looks_like_resume(_read("not_a_resume.txt").decode())


def test_roles_from_skills() -> None:
    assert roles_from_skills(["React", "FastAPI", "PostgreSQL"])[0] == "Full Stack Developer"
    assert roles_from_skills([]) == ["Software Engineer"]


def test_simple_corrections_without_ai() -> None:
    p = factories.profile(location_current=None, gaps_or_flags=["no location found"])
    fixed = apply_simple_correction(
        p, "I'm in Bengaluru. Add Docker and AWS to my skills, remove Django"
    )
    assert fixed.location_current == "Bengaluru"
    assert "AWS" in fixed.skills.cloud
    assert "Django" not in fixed.skills.all()
    assert not fixed.gaps_or_flags
