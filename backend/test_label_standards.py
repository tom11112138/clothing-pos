"""Category reference coverage; recommendations never certify a real garment."""
import unittest
from urllib.parse import urlparse

from label_standards import EXECUTION_STANDARDS, label_issues, normalize_category, standards_catalog


class StandardsCatalogTests(unittest.TestCase):
    def test_all_twelve_categories_have_specific_standard_candidates(self):
        catalog = standards_catalog()
        names = [category["name"] for category in catalog["categories"]]
        self.assertEqual(names, ["羊绒衫", "牛仔裤", "风衣", "连衣裙", "羽绒服", "羊绒大衣",
                                 "休闲裤", "衬衫", "半袖", "棉服", "套装", "针织衫"])
        codes = [item["code"] for item in catalog["execution_standards"]]
        self.assertEqual(len(codes), len(set(codes)))
        self.assertEqual(len(codes), 19)
        for category in catalog["categories"]:
            with self.subTest(category=category["name"]):
                self.assertTrue(category["note"])
                self.assertTrue(any(category["name"] in item["categories"] for item in EXECUTION_STANDARDS))
        for standard in EXECUTION_STANDARDS:
            self.assertTrue(set(standard["categories"]).issubset(names))
            self.assertTrue(standard["scope"])
            self.assertIn(urlparse(standard["source"]).hostname, {"std.samr.gov.cn", "openstd.samr.gov.cn"})

    def test_woven_and_knitted_candidates_are_not_conflated(self):
        expected = {
            "牛仔裤": {"FZ/T 81006-2017", "FZ/T 73032-2017"},
            "连衣裙": {"FZ/T 81004-2022", "FZ/T 73026-2014"},
            "羽绒服": {"GB/T 14272-2021", "FZ/T 73053-2015"},
            "半袖": {"GB/T 22849-2024", "GB/T 2660-2017", "FZ/T 73043-2020"},
        }
        for category, codes in expected.items():
            self.assertEqual({item["code"] for item in EXECUTION_STANDARDS if category in item["categories"]}, codes)
        coats = {item["code"] for item in EXECUTION_STANDARDS if "羊绒大衣" in item["categories"]}
        self.assertNotIn("FZ/T 73009-2021", coats)
        self.assertIn("FZ/T 73058-2017", coats)

    def test_future_replacements_are_not_presented_as_current_choices(self):
        codes = {item["code"] for item in EXECUTION_STANDARDS}
        self.assertFalse(codes & {"GB/T 2664-2026", "GB/T 2665-2026", "FZ/T 73053-2025", "FZ/T 73026-2025"})
        for code in ("GB/T 2664-2017", "GB/T 2665-2017", "FZ/T 73053-2015", "FZ/T 73026-2014"):
            self.assertIn("实施", next(item for item in EXECUTION_STANDARDS if item["code"] == code)["note"])

    def test_safety_categories_include_conditions_examples_and_child_rules(self):
        catalog = standards_catalog()
        self.assertEqual([item["value"] for item in catalog["safety_categories"]], ["A", "B", "C"])
        for item in catalog["safety_categories"]:
            self.assertTrue(item["description"])
            self.assertTrue(item["examples"])
        usages = {item["value"]: item for item in catalog["usages"]}
        self.assertIn("贴身羊绒衫", usages["adult_skin"]["description"])
        self.assertIn("隔着内搭", usages["adult_outer"]["description"])
        self.assertEqual(usages["infant"]["minimum"], "A")
        self.assertIn("额外安全要求", usages["child_skin"]["description"])

    def test_catalog_exclusions_also_guard_confirmed_infant_labels(self):
        config = {"composition": "100%棉", "execution_standard": "", "label_usage": "infant",
                  "safety_category": "A", "label_verified": True}
        for standard in EXECUTION_STANDARDS:
            if standard.get("excludes_infant"):
                with self.subTest(standard=standard["code"]):
                    config["execution_standard"] = standard["code"].lower().replace(" ", "").replace("-", "—")
                    self.assertTrue(any("不适用于" in issue for issue in label_issues(config)))

    def test_category_normalization_preserves_legacy_and_custom_values(self):
        self.assertEqual(normalize_category(" 莲衣裙 "), "连衣裙")
        self.assertEqual(normalize_category("上衣"), "上衣")
        self.assertEqual(normalize_category("自定义品类"), "自定义品类")
        self.assertIsNone(normalize_category(None))


if __name__ == "__main__":
    unittest.main()
