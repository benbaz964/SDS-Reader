"""
Regression tests: every expected value below was checked by hand against
the source PDF's own text. Run with:  python -m unittest discover tests

Real supplier SDSs (test_data_batch4/) aren't redistributed in the repo, so
their tests are skipped automatically where those files aren't present.
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sds_reader.extractor import extract_sds  # noqa: E402
from sds_reader.target_format import build_target_format  # noqa: E402

_cache = {}


def load(rel_path):
    path = os.path.join(ROOT, rel_path)
    if not os.path.isfile(path):
        raise unittest.SkipTest(f"{rel_path} not present")
    if path not in _cache:
        rec = extract_sds(path)
        _cache[path] = (build_target_format(rec.values), rec.values.get("substance_breakdown") or [])
    return _cache[path]


def flags(row):
    return (row["carcinogenic"], row["mutagenic"], row["reproductive_toxicant"], row["sensitiser"])


NONE = ("Not stated",) * 4


class SyntheticSheets(unittest.TestCase):
    def test_acetonitrile_revision_not_print_date(self):
        f, _ = load("test_data_batch2/02_sigma_aldrich_acetonitrile.pdf")
        self.assertEqual(f["SDS Date"], "03/22/2026")
        self.assertEqual(f["SDS Version"], "6.3")
        self.assertNotIn("\n", f["PPE_RPE"])

    def test_basf_date_pictogram_combined_heading(self):
        f, _ = load("test_data_batch2/03_basf_eu_reach_dispersant.pdf")
        self.assertEqual(f["SDS Date"], "14.01.2026")
        self.assertEqual(f["Hazard Pictogram"], "GHS07")
        self.assertEqual(f["Carcinogenetic"], "Not classified based on available data.")
        self.assertIn("Not classified", f["Mutagens"])
        self.assertEqual(f["PPE_Skin"], "Body protection according to the degree of exposure.")

    def test_pre_ghs_sheet_salvages_basics(self):
        f, b = load("test_data_batch2/04_old_pre_ghs_msds.pdf")
        self.assertEqual(f["Substance Name"], "Old Formula Degreaser Concentrate")
        self.assertEqual([(r["name"], r["cas"], r["concentration"]) for r in b],
                         [("1,1,1-Trichloroethane", "71-55-6", "90%")])

    def test_multi_ingredient_flags_only_cadmium(self):
        _, b = load("test_data_batch2/06_multipage_composition_table.pdf")
        self.assertEqual(len(b), 8)
        for r in b:
            expected = ("Yes", "Not stated", "Not stated", "Not stated") if r["name"] == "Cadmium oxide" else NONE
            self.assertEqual(flags(r), expected, r["name"])

    def test_paint_tio2_not_a_sensitiser(self):
        _, b = load("test_data_batch2/08_paint_coating_procoat.pdf")
        tio2 = next(r for r in b if r["name"] == "Titanium dioxide")
        self.assertEqual(flags(tio2), ("Yes", "Not stated", "Not stated", "Not stated"))

    def test_welding_rod_only_nickel_sensitiser(self):
        _, b = load("test_data_batch2/09_metal_alloy_welding_rod.pdf")
        by = {r["name"]: flags(r) for r in b}
        self.assertEqual(by["Nickel"], ("Yes", "Not stated", "Not stated", "Yes"))
        self.assertEqual(by["Chromium"], ("Yes", "Not stated", "Not stated", "Not stated"))
        self.assertEqual(by["Iron"], NONE)

    def test_negated_statements_not_flagged(self):
        f, b = load("test_data_batch2/10_pharma_excipient_hec.pdf")
        self.assertEqual(flags(b[0]), NONE)
        self.assertEqual(f["Reproductive Toxins"], "No reproductive or developmental toxicity observed in animal studies.")

    def test_wrapped_prose_ingredient_name(self):
        _, b = load("test_data_batch2/11_prose_only_epoxy.pdf")
        self.assertEqual([r["name"] for r in b],
                         ["Bisphenol A diglycidyl ether", "Reactive diluent (1,4-butanediol diglycidyl ether)"])

    def test_acme_not_classified_is_not_carcinogen(self):
        f, b = load("test_data/Acme_Solvent_X_SDS.pdf")
        self.assertEqual(flags(b[0]), NONE)
        self.assertEqual((b[0]["ltel"], b[1]["stel"]), ("20 ppm", "150 ppm"))
        self.assertEqual(f["Hazard Pictogram"], "Flame, Exclamation mark")

    def test_aluminium(self):
        f, b = load("test_data_batch3/Aluminium_Powder_SDS.pdf")
        self.assertEqual(f["Hazard Pictogram"], "GHS02")
        self.assertEqual(f["SDS Date"], "03/06/2019")
        self.assertEqual(flags(b[0]), NONE)

    def test_argon_physical_property_is_not_a_vapour_hazard(self):
        f, _ = load("test_data_batch2/01_gas_cylinder_argon.pdf")
        self.assertEqual(f["Vapour"], "Not stated in SDS")


class SupplierSheets(unittest.TestCase):
    def test_renoclean_repeated_answers_survive(self):
        f, b = load("test_data_batch4/renoclean_mso3006.pdf")
        self.assertEqual(f["Substance Name"], "RENOCLEAN MSO 3006")
        self.assertIn("classification criteria are not met", f["Carcinogenetic"])
        self.assertIn("Use skin protection cream", f["PPE_Hands"])
        hex_ = next(r for r in b if r["name"] == "Hexylen glycol")
        self.assertEqual((hex_["ltel"], hex_["wel_type"]), ("25 ppm 123 mg/m3", "WEL (EH40)"))
        for r in b:
            self.assertEqual(flags(r), NONE, r["name"])

    def test_capa_not_available_answers_kept(self):
        f, _ = load("test_data_batch4/capa_6800.pdf")
        self.assertEqual(f["Exposure limits"], "None.")
        self.assertEqual(f["Melting Point"], "58 to 60")
        self.assertTrue(f["Sensitiser"].startswith("Sensitization: Not available"))
        self.assertTrue(f["PPE_RPE"].endswith("aspects of use."))

    def test_ipa(self):
        f, b = load("test_data_batch4/isopropyl-alcohol.pdf")
        self.assertEqual(b[0]["name"], "Isopropyl alcohol")
        self.assertEqual(flags(b[0]), NONE)
        self.assertEqual(f["Melting Point"], "-89,5")
        self.assertIn("TLV-TWA 980 mg/m3", f["Exposure limits"])
        self.assertIn("Nitrile rubber", f["PPE_Hands"])
        self.assertEqual(f["PPE_Skin"], "Flame retardant antistatic protective clothing.")

    def test_barium_limits_from_text(self):
        f, b = load("test_data_batch4/barium-chloride-dihydrate-certified-ar-for-analysis-fisher-chemicaltrade.pdf")
        self.assertTrue(f["Exposure limits"].startswith("Barium chloride STEL: 1.5 mg/m3"))
        self.assertEqual((b[0]["ltel"], b[0]["stel"]), ("0.5 mg/m3", "1.5 mg/m3"))
        self.assertEqual(f["Fume"], "Not stated in SDS")

    def test_bartoline(self):
        f, b = load("test_data_batch4/bartoline_white_spirit.pdf")
        self.assertEqual(f["Boiling Point"], "158 – 191")
        self.assertNotIn("100% aromatics", b[0]["name"])
        self.assertEqual(b[0]["ltel"], "350 mg/m3")
        self.assertNotIn("Hygiene measures", f["PPE_Skin"])

    def test_adrana_all_composition_blocks(self):
        f, b = load("test_data_batch4/adrana_d208.pdf")
        self.assertEqual(len(b), 10)
        self.assertNotIn("Polska:", f["Exposure limits"])
        self.assertEqual(f["Sensitiser"], "Sensitization by inhalation is not expected.\n"
                                          "Sensitization by skin contact is not expected.")

    def test_indopol_negated_component_statements(self):
        f, b = load("test_data_batch4/indopol_h300.pdf")
        self.assertEqual(flags(b[0]), NONE)
        self.assertNotIn("indopol", f["PPE_RPE"])


if __name__ == "__main__":
    unittest.main()
