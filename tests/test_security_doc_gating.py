from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pm_sim.actions import read_doc, send_chat
from pm_sim.db import connect
from pm_sim.jsonutil import loads
from pm_sim.paths import DEFAULT_SCENARIO_PATH
from pm_sim.state import reset
from pm_sim.engine.time import advance_time


class SecurityDocGatingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        reset(self.db_path, DEFAULT_SCENARIO_PATH)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_private_repo_security_doc_requires_daisy_security_question(self) -> None:
        hidden = read_doc(self.db_path, "doc_private_repo_security_baseline")

        send_chat(
            self.db_path,
            "luigi",
            "Nimbus asked if we store source code from private repos. Is there a security doc?",
        )
        advance_time(self.db_path, "until_next_event")
        still_hidden = read_doc(self.db_path, "doc_private_repo_security_baseline")

        advance_time(self.db_path, "to:2026-06-24T14:00:00")
        send_chat(
            self.db_path,
            "luigi",
            "Nimbus asked if we store source code from private repos. Is there a security doc?",
        )
        advance_time(self.db_path, "to:2026-06-24T16:00:00")
        revealed = read_doc(self.db_path, "doc_private_repo_security_baseline")

        conn = connect(self.db_path)
        try:
            state = conn.execute(
                """
                SELECT value_json
                FROM coworker_state
                WHERE person_id = 'luigi'
                  AND key = 'security_doc_shared'
                """
            ).fetchone()
        finally:
            conn.close()

        self.assertFalse(hidden["ok"])
        self.assertFalse(still_hidden["ok"])
        self.assertTrue(revealed["ok"])
        self.assertIn("Raw source code is not stored long term", revealed["doc"]["body"])
        self.assertTrue(loads(state["value_json"]))

    def test_koopa_security_review_message_does_not_reveal_private_repo_doc(self) -> None:
        hidden = read_doc(self.db_path, "doc_private_repo_security_baseline")

        send_chat(
            self.db_path,
            "luigi",
            (
                "Need grounded inputs for this week’s launch and Koopa scope decisions. "
                "What is the repo-sync stale-commit risk, and is a one-time admin audit-log "
                "CSV feasible before Koopa's Thursday security review?"
            ),
        )
        advance_time(self.db_path, "until_next_event")
        still_hidden = read_doc(self.db_path, "doc_private_repo_security_baseline")

        self.assertFalse(hidden["ok"])
        self.assertFalse(still_hidden["ok"])
