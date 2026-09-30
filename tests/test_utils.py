import unittest

from bugbot.sheets import GoogleSheetStore
from bugbot.utils import (
    clean_description,
    is_checked,
    telegram_message_url,
    topic_is_allowed,
)


class UtilsTests(unittest.TestCase):
    def test_topic_filter(self) -> None:
        allowed = frozenset({101, 202})
        self.assertTrue(topic_is_allowed(-1001, 101, -1001, allowed))
        self.assertFalse(topic_is_allowed(-1001, 303, -1001, allowed))
        self.assertFalse(topic_is_allowed(-1001, None, -1001, allowed))
        self.assertFalse(topic_is_allowed(-1002, 101, -1001, allowed))

    def test_clean_description_removes_tag(self) -> None:
        self.assertEqual(
            clean_description("Ошибка кнопки\n#new_bug", "#new_bug"),
            "Ошибка кнопки",
        )

    def test_clean_description_is_case_insensitive(self) -> None:
        self.assertEqual(
            clean_description("Не работает #NEW_BUG", "#new_bug"),
            "Не работает",
        )

    def test_private_supergroup_link(self) -> None:
        self.assertEqual(
            telegram_message_url(-1001234567890, None, 42),
            "https://t.me/c/1234567890/42",
        )

    def test_public_group_link(self) -> None:
        self.assertEqual(
            telegram_message_url(-1001, "example_group", 7),
            "https://t.me/example_group/7",
        )

    def test_checkbox_values(self) -> None:
        self.assertTrue(is_checked(True))
        self.assertTrue(is_checked("TRUE"))
        self.assertFalse(is_checked(False))

    def test_legacy_row_migration(self) -> None:
        message_url = "https://t.me/c/123/45"
        row = [
            7,
            f'=HYPERLINK("{message_url}","Открыть фото")',
            "Описание",
            False,
            "",
            "-100123",
            "45",
            "photo-file-id",
        ]

        migrated = GoogleSheetStore._migrate_legacy_row(row)

        self.assertEqual(
            migrated[0],
            f'=HYPERLINK("{message_url}","Открыть сообщение")',
        )
        self.assertEqual(migrated[1], "")
        self.assertEqual(migrated[2], "Описание")
        self.assertEqual(migrated[9], 7)
        self.assertEqual(migrated[10], message_url)


if __name__ == "__main__":
    unittest.main()
