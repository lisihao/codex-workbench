from copy import deepcopy
import unittest

from codex_workbench.routing import route_task
from tests.test_routing import make_contract, v3_catalog


def catalog_with_astra():
    catalog = v3_catalog()
    astra = deepcopy(catalog["models"][0])
    astra.update(model_id="gpt-6-astra", capability_id="codex:gpt-6-astra")
    catalog["models"].append(astra)
    return catalog


class AstraRoutingIntegrationTests(unittest.TestCase):
    def test_explicit_astra_control_choice_survives_top_level_routing(self):
        catalog = catalog_with_astra()
        contract = make_contract(planner_model="gpt-6-astra", verifier_model="gpt-6-astra")
        for role in ("planner", "verifier", "control"):
            with self.subTest(role=role):
                route = route_task(contract, role=role, capability_snapshot=catalog)
                self.assertEqual(route.model, "gpt-6-astra")
                self.assertEqual(route.model_reasoning_effort, "max")

    def test_default_sol_remains_exact_when_astra_is_available(self):
        route = route_task(make_contract(), role="planner", capability_snapshot=catalog_with_astra())
        self.assertEqual(route.model, "gpt-5.6-sol")

    def test_missing_astra_does_not_silently_substitute_sol(self):
        with self.assertRaises(ValueError):
            route_task(make_contract(planner_model="gpt-6-astra"), role="planner", capability_snapshot=v3_catalog())

    def test_legacy_contract_retains_explicit_astra_identity(self):
        route = route_task(make_contract(verifier_model="gpt-6-astra"), role="verifier")
        self.assertEqual(route.model, "gpt-6-astra")


if __name__ == "__main__":
    unittest.main()
