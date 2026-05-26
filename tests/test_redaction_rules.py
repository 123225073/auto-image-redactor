import unittest

from csdn_image_mosaic import (
    OcrBox,
    extract_redaction_terms_from_instruction,
    find_local_mask_ids,
    instruction_terms_for_matching,
    is_additive_instruction,
    is_no_mask_instruction,
    is_strict_only_instruction,
    load_terms,
)


DEFAULT_BUSINESS_TERMS = [
    "\u8d5b\u90a6",
    "\u4e50\u571f",
    "\u7eaf\u7c73",
    "xxx\u516c\u53f8",
    "xxx\u80a1\u4efd\u6709\u9650\u516c\u53f8",
]


def box(identifier: int, text: str, x: int = 0, y: int = 0, width: int = 100, height: int = 20) -> OcrBox:
    return OcrBox(identifier, text, 0.99, (x, y, x + width, y + height))


class RedactionRulesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.terms = load_terms(None) + DEFAULT_BUSINESS_TERMS

    def assert_masked_ids(self, boxes: list[OcrBox], expected: list[int], terms: list[str] | None = None) -> None:
        actual = sorted(find_local_mask_ids(boxes, terms or self.terms, False))
        self.assertEqual(actual, expected)

    def test_sap_material_numbers_are_not_redacted_by_default(self) -> None:
        self.assert_masked_ids([box(1, "11206030010002")], [])
        self.assert_masked_ids([box(1, "112060300100021111")], [])
        self.assert_masked_ids([box(1, "BOM\u7ec4\u4ef6\u4e2d\u90fd\u67090\u5c42\u7684\u7269\u659911206030010002")], [])

    def test_sap_material_label_overrides_identity_like_number(self) -> None:
        self.assert_masked_ids(
            [
                box(1, "\u7269\u6599\u53f7"),
                box(2, "110101199001010011", 110, 0),
            ],
            [],
        )

    def test_security_values_next_to_sensitive_labels_are_redacted(self) -> None:
        self.assert_masked_ids(
            [
                box(1, "\u94f6\u884c\u8d26\u53f7"),
                box(2, "6222021234567890123", 110, 0),
            ],
            [2],
        )
        self.assert_masked_ids(
            [
                box(1, "\u5bc6\u7801"),
                box(2, "abc123", 110, 0),
            ],
            [2],
        )
        self.assert_masked_ids(
            [
                box(1, "\u7a0e\u53f7"),
                box(2, "91310000MA1K12345X", 110, 0),
            ],
            [2],
        )

    def test_company_terms_and_explicit_user_terms_still_win(self) -> None:
        self.assert_masked_ids([box(1, "C050\u5e7f\u4e1c\u7eaf\u7c73\u5de5\u5382")], [1])
        self.assert_masked_ids(
            [box(1, "11206030010002")],
            [1],
            self.terms + ["11206030010002"],
        )

    def test_single_image_instruction_adds_temporary_redaction_terms(self) -> None:
        instruction = "\u6c5f\u95e8\u4e50\u7c73 \u4e5f\u6253\u7801"
        self.assertEqual(extract_redaction_terms_from_instruction(instruction), ["\u6c5f\u95e8\u4e50\u7c73"])
        self.assertEqual(
            extract_redaction_terms_from_instruction("\u4e0d\u8981\u6253\u7801 SAP \u6807\u51c6\u5b57\u6bb5\uff1b\u6c5f\u95e8\u4e50\u7c73\u4e5f\u6253\u7801"),
            ["\u6c5f\u95e8\u4e50\u7c73"],
        )
        self.assert_masked_ids(
            [box(1, "1 \u6c5f\u95e8\u4e50\u7c73")],
            [1],
            self.terms + instruction_terms_for_matching(instruction, "fuzzy"),
        )

    def test_no_mask_instruction_does_not_swallow_exclusion_rules(self) -> None:
        self.assertTrue(is_no_mask_instruction("\u8fd9\u5f20\u56fe\u4e0d\u9700\u8981\u6253\u7801"))
        self.assertFalse(is_no_mask_instruction("\u4e0d\u8981\u6253\u7801 SAP \u6807\u51c6\u5b57\u6bb5"))

    def test_rerun_instruction_modes(self) -> None:
        additive = "\u4fdd\u7559\u539f\u56fe\u5df2\u6253\u7801\u90e8\u5206\uff0c\u8865\u5145\u6253\u7801\u6c5f\u95e8\u4e50\u7c73"
        strict = "\u5176\u4ed6\u4e0d\u9700\u8981\u6253\u7801\uff0c\u53ea\u9700\u8981\u6253\u7801\u6c5f\u95e8\u4e50\u7c73"
        direct = "\u6253\u7801\u6c5f\u95e8\u4e50\u7c73"
        self.assertTrue(is_additive_instruction(additive))
        self.assertFalse(is_strict_only_instruction(additive))
        self.assertTrue(is_strict_only_instruction(strict))
        self.assertFalse(is_additive_instruction(strict))
        self.assertEqual(extract_redaction_terms_from_instruction(additive), ["\u6c5f\u95e8\u4e50\u7c73"])
        self.assertEqual(extract_redaction_terms_from_instruction(strict), ["\u6c5f\u95e8\u4e50\u7c73"])
        self.assertEqual(extract_redaction_terms_from_instruction(direct), ["\u6c5f\u95e8\u4e50\u7c73"])


if __name__ == "__main__":
    unittest.main()
