import threading
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
            clean_description("Ошибка кнопки\n#bug", "#bug"),
            "Ошибка кнопки",
        )

    def test_clean_description_is_case_insensitive(self) -> None:
        self.assertEqual(
            clean_description("Не работает #BUG", "#bug"),
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
            message_url,
        )
        self.assertEqual(migrated[1], "Описание")
        self.assertEqual(migrated[2], "")
        self.assertEqual(migrated[9], 7)

    def test_photo_layout_migration_removes_photo_column(self) -> None:
        row = [
            "link",
            "photo",
            "Описание",
            True,
            False,
            "-100123",
            "45",
            "photo-file-id",
            True,
            9,
            "https://t.me/c/123/45",
        ]

        migrated = GoogleSheetStore._migrate_photo_row(row)

        self.assertEqual(len(migrated), 10)
        self.assertEqual(migrated[0], "link")
        self.assertEqual(migrated[1], "Описание")
        self.assertEqual(migrated[2], "")
        self.assertEqual(migrated[3:5], [True, False])
        self.assertNotIn("photo", migrated)
        self.assertNotIn("photo-file-id", migrated)

    def test_assignment_tracking_migration_preserves_workflow(self) -> None:
        row = [
            "link",
            "Описание",
            "Фронт",
            True,
            False,
            "-100",
            "1",
            True,
            4,
        ]

        migrated = GoogleSheetStore._migrate_pre_assignment_row(row)

        self.assertEqual(
            migrated,
            ["link", "Описание", "Фронт", True, False, "-100", "1", False, True, 4],
        )

    def test_competence_migration_preserves_workflow(self) -> None:
        row = ["link", "Описание", True, False, "-100", "1", True, 4, "url"]

        migrated = GoogleSheetStore._migrate_pre_competence_row(row)

        self.assertEqual(
            migrated,
            ["link", "Описание", "", True, False, "-100", "1", False, True, 4],
        )

    def test_next_row_ignores_empty_checkbox_rows(self) -> None:
        rows = [
            ["", "Первый баг", "", False, False, "-100", "1", False, False, 1],
            ["", "Второй баг", "", False, False, "-100", "2", False, False, 2],
            ["", "", "", False, False],
            ["", "", "", False, False],
        ]

        self.assertEqual(GoogleSheetStore._next_available_row(rows), 4)

    def test_row_url_opens_visible_bug_cells(self) -> None:
        store = object.__new__(GoogleSheetStore)
        store._spreadsheet_id = "sheet-id"
        store._sheet_id = 123

        self.assertEqual(
            store._row_url(7),
            "https://docs.google.com/spreadsheets/d/sheet-id/edit#gid=123&range=A7:E7",
        )

    def test_competence_notification_uses_assignment_flag(self) -> None:
        store = object.__new__(GoogleSheetStore)
        store._lock = threading.RLock()
        store._spreadsheet_id = "sheet-id"
        store._sheet_id = 123
        store._read_rows = lambda: [
            [
                "link",
                "Описание",
                "Фронт",
                False,
                False,
                "-100",
                "7",
                False,
                False,
                3,
            ]
        ]

        pending = store.get_pending_competence_notifications()

        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].competence, "Фронт")
        self.assertEqual(pending[0].issue_id, 3)
        self.assertEqual(pending[0].row_url, store._row_url(2))

    def test_fix_notification_uses_separate_fix_flag(self) -> None:
        store = object.__new__(GoogleSheetStore)
        store._lock = threading.RLock()
        store._spreadsheet_id = "sheet-id"
        store._sheet_id = 123
        store._read_rows = lambda: [
            [
                "link",
                "Описание",
                "Бэкенд",
                True,
                False,
                "-100",
                "7",
                True,
                False,
                3,
            ]
        ]

        pending = store.get_pending_fix_notifications()

        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].issue_id, 3)
        self.assertEqual(pending[0].row_url, store._row_url(2))


if __name__ == "__main__":
    unittest.main()
