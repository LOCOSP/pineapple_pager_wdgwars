import unittest

from . import conftest_path  # noqa: F401
from scanners.wigle_auth import (
    AKM_EAP, AKM_EAP_SUITE_B, AKM_OWE, AKM_PSK, AKM_SAE, build_auth,
    join_ciphers,
)


class TestJoinCiphers(unittest.TestCase):
    def test_empty_defaults_to_ccmp(self):
        self.assertEqual(join_ciphers([]), "CCMP")
        self.assertEqual(join_ciphers(None), "CCMP")

    def test_stable_order_regardless_of_input_order(self):
        self.assertEqual(join_ciphers(["TKIP", "CCMP"]), "CCMP+TKIP")
        self.assertEqual(join_ciphers(["CCMP", "TKIP"]), "CCMP+TKIP")


class TestBuildAuth(unittest.TestCase):
    def test_open_network(self):
        self.assertEqual(build_auth(privacy=False), "[ESS]")

    def test_wep(self):
        self.assertEqual(build_auth(privacy=True), "[WEP][ESS]")

    def test_ibss(self):
        self.assertEqual(build_auth(privacy=False, ess=False), "[IBSS]")

    def test_wpa2_psk(self):
        got = build_auth(privacy=True,
                         rsn={"ciphers": ["CCMP"], "akms": {AKM_PSK}})
        self.assertEqual(got, "[WPA2-PSK-CCMP][ESS]")

    def test_enterprise_is_not_reported_as_psk(self):
        got = build_auth(privacy=True,
                         rsn={"ciphers": ["CCMP"], "akms": {AKM_EAP}})
        self.assertEqual(got, "[WPA2-EAP-CCMP][ESS]")
        self.assertNotIn("PSK", got)

    def test_wpa3_sae(self):
        got = build_auth(privacy=True,
                         rsn={"ciphers": ["CCMP"], "akms": {AKM_SAE}})
        self.assertEqual(got, "[WPA3-SAE-CCMP][ESS]")

    def test_wpa3_transition_reports_both(self):
        got = build_auth(privacy=True,
                         rsn={"ciphers": ["CCMP"], "akms": {AKM_SAE, AKM_PSK}})
        self.assertIn("[WPA3-SAE-CCMP]", got)
        self.assertIn("[WPA2-PSK-CCMP]", got)

    def test_owe(self):
        got = build_auth(privacy=True,
                         rsn={"ciphers": ["CCMP"], "akms": {AKM_OWE}})
        self.assertEqual(got, "[WPA3-OWE-CCMP][ESS]")
        self.assertNotIn("PSK", got)

    def test_suite_b_is_wpa3_enterprise(self):
        got = build_auth(privacy=True,
                         rsn={"ciphers": ["GCMP-256"],
                              "akms": {AKM_EAP, AKM_EAP_SUITE_B}})
        self.assertEqual(got, "[WPA3-EAP-GCMP-256][ESS]")

    def test_wps_flag(self):
        got = build_auth(privacy=True,
                         rsn={"ciphers": ["CCMP"], "akms": {AKM_PSK}},
                         wps=True)
        self.assertEqual(got, "[WPA2-PSK-CCMP][WPS][ESS]")

    def test_mixed_wpa_and_rsn_order(self):
        got = build_auth(
            privacy=True,
            wpa={"ciphers": ["TKIP"], "akms": {AKM_PSK}},
            rsn={"ciphers": ["CCMP", "TKIP"], "akms": {AKM_PSK}},
        )
        # WPA1 group comes before the RSN group, as Android emits it.
        self.assertEqual(got, "[WPA-PSK-TKIP][WPA2-PSK-CCMP+TKIP][ESS]")

    def test_unparsed_rsn_falls_back_to_psk(self):
        got = build_auth(privacy=True, rsn={"ciphers": [], "akms": set()})
        self.assertEqual(got, "[WPA2-PSK-CCMP][ESS]")


if __name__ == "__main__":
    unittest.main()
