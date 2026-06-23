from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pm_sim.db import connect
from pm_sim.engine.conditions import condition_matches
from pm_sim.engine.effects import apply_effects
from pm_sim.engine.time import advance_time
from pm_sim.paths import DEFAULT_SCENARIO_PATH
from pm_sim.state import observe, reset


class PressureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        reset(self.db_path, DEFAULT_SCENARIO_PATH)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_pressure_effects_mutate_bounded_pressure_state(self) -> None:
        conn = connect(self.db_path)
        try:
            raised = apply_effects(
                conn,
                [
                    {
                        "type": "increase_pressure",
                        "pressure_id": "pressure_nimbus_customer_confidence",
                        "by": 9,
                        "reason": "Customer update window was missed.",
                    }
                ],
                now="2026-06-25T10:00:00",
                source="test",
            )
            lowered = apply_effects(
                conn,
                [
                    {
                        "type": "lower_pressure",
                        "pressure_id": "pressure_nimbus_customer_confidence",
                        "to": 2,
                        "reason": "Daisy received grounded wording.",
                    }
                ],
                now="2026-06-25T10:20:00",
                source="test",
            )
            conn.commit()

            row = conn.execute(
                """
                SELECT intensity, reason, updated_at
                FROM pressures
                WHERE id = 'pressure_nimbus_customer_confidence'
                """
            ).fetchone()

            self.assertEqual(raised[0]["intensity"], 10)
            self.assertEqual(lowered[0]["previous_intensity"], 10)
            self.assertEqual(row["intensity"], 2)
            self.assertEqual(row["reason"], "Daisy received grounded wording.")
            self.assertEqual(row["updated_at"], "2026-06-25T10:20:00")
            self.assertTrue(
                condition_matches(
                    conn,
                    {
                        "pressure_at_most": {
                            "id": "pressure_nimbus_customer_confidence",
                            "intensity": 3,
                        }
                    },
                )
            )
        finally:
            conn.close()

    def test_background_event_raises_pressure_without_project_delta_metadata(self) -> None:
        before = observe(self.db_path)
        advance_time(self.db_path, "to:2026-06-24T15:30:00")
        after = observe(self.db_path)

        before_pressures = {row["id"]: row for row in before["pressures"]}
        after_pressures = {row["id"]: row for row in after["pressures"]}

        self.assertGreater(
            after_pressures["pressure_nimbus_customer_confidence"]["intensity"],
            before_pressures["pressure_nimbus_customer_confidence"]["intensity"],
        )
        for project in after["projects"]:
            self.assertNotIn("stakeholder_pressure_delta", project["metadata_json"])
            self.assertNotIn("scope_pressure_delta", project["metadata_json"])


if __name__ == "__main__":
    unittest.main()
