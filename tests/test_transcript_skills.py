"""Tests for transcript skill listing extraction."""

from __future__ import annotations

import json

from honeyhive_daemon.transcript import clear_transcript_cache, get_skills_listing


def test_get_skills_listing_from_attachment(tmp_path) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        '{"type":"user","message":{"role":"user","content":"hi"}}\n'
        + json.dumps(
            {
                "type": "attachment",
                "attachment": {
                    "type": "skill_listing",
                    "names": ["hh-daemon-smoke", "other-skill"],
                    "skillCount": 2,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    clear_transcript_cache()

    listing = get_skills_listing(str(transcript))
    assert listing is not None
    assert listing["count"] == 2
    assert listing["names"] == ["hh-daemon-smoke", "other-skill"]
