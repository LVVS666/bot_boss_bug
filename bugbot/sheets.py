from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build

from .utils import is_checked


SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
HEADERS = [
    "Ссылка на сообщение",
    "Фотография",
    "Описание бага",
    "Подтверждение исправления",
    "Исправлено",
    "source_chat_id",
    "source_message_id",
    "telegram_file_id",
    "fix_notification_sent",
    "issue_id",
    "message_url",
]
LEGACY_HEADERS = [
    "№",
    "Фото",
    "Описание проблемы",
    "Удалить",
    "Примечание",
    "chat_id",
    "message_id",
    "telegram_file_id",
]


@dataclass(frozen=True)
class AppendResult:
    issue_id: int
    row_number: int


@dataclass(frozen=True)
class FixNotification:
    issue_id: int
    description: str
    message_url: str
    telegram_file_id: str
    source_chat_id: int
    source_message_id: int


class GoogleSheetStore:
    def __init__(
        self,
        credentials_info: dict[str, Any],
        spreadsheet_id: str,
        sheet_name: str | None,
    ) -> None:
        credentials = service_account.Credentials.from_service_account_info(
            credentials_info,
            scopes=SCOPES,
        )
        self._service = build(
            "sheets",
            "v4",
            credentials=credentials,
            cache_discovery=False,
        )
        self._spreadsheet_id = spreadsheet_id
        self._requested_sheet_name = sheet_name
        self._sheet_name = ""
        self._sheet_id = 0
        self._lock = threading.RLock()

    @property
    def sheet_name(self) -> str:
        return self._sheet_name

    def initialize(self) -> None:
        with self._lock:
            metadata = (
                self._service.spreadsheets()
                .get(spreadsheetId=self._spreadsheet_id)
                .execute()
            )
            sheets = metadata.get("sheets", [])
            if not sheets:
                raise RuntimeError("The spreadsheet has no worksheets")

            selected = None
            if self._requested_sheet_name:
                selected = next(
                    (
                        item
                        for item in sheets
                        if item["properties"]["title"] == self._requested_sheet_name
                    ),
                    None,
                )
                if selected is None:
                    raise RuntimeError(
                        f"Worksheet {self._requested_sheet_name!r} was not found"
                    )
            else:
                selected = sheets[0]

            properties = selected["properties"]
            self._sheet_name = properties["title"]
            self._sheet_id = int(properties["sheetId"])
            self._ensure_layout()

    def append_bug(
        self,
        description: str,
        message_url: str | None,
        chat_id: int,
        message_id: int,
        telegram_file_id: str = "",
    ) -> AppendResult:
        with self._lock:
            rows = self._read_rows()
            if self._is_duplicate(rows, chat_id, message_id):
                raise DuplicateMessageError(chat_id, message_id)

            issue_id = self._next_issue_id(rows)
            link_value = self._link_formula(message_url) if message_url else ""
            response = (
                self._service.spreadsheets()
                .values()
                .append(
                    spreadsheetId=self._spreadsheet_id,
                    range=f"'{self._quoted_sheet_name()}'!A:K",
                    valueInputOption="USER_ENTERED",
                    insertDataOption="INSERT_ROWS",
                    includeValuesInResponse=True,
                    body={
                        "majorDimension": "ROWS",
                        "values": [[
                            link_value,
                            "",
                            self._safe_user_text(description),
                            False,
                            False,
                            str(chat_id),
                            str(message_id),
                            telegram_file_id,
                            False,
                            issue_id,
                            message_url or "",
                        ]],
                    },
                )
                .execute()
            )
            updated_range = response["updates"]["updatedRange"]
            row_number = self._parse_row_number(updated_range)
            self._format_new_row(row_number)
            return AppendResult(issue_id=issue_id, row_number=row_number)

    def get_pending_fix_notifications(self) -> list[FixNotification]:
        with self._lock:
            pending: list[FixNotification] = []
            for values in self._read_rows():
                developer_confirmed = values[3] if len(values) > 3 else False
                tester_confirmed = values[4] if len(values) > 4 else False
                already_sent = values[8] if len(values) > 8 else False
                if (
                    is_checked(developer_confirmed)
                    and not is_checked(tester_confirmed)
                    and not is_checked(already_sent)
                ):
                    pending.append(
                        FixNotification(
                            issue_id=self._safe_int(values[9] if len(values) > 9 else 0),
                            description=str(values[2]) if len(values) > 2 else "",
                            message_url=str(values[10]) if len(values) > 10 else "",
                            telegram_file_id=str(values[7]) if len(values) > 7 else "",
                            source_chat_id=self._safe_int(values[5] if len(values) > 5 else 0),
                            source_message_id=self._safe_int(values[6] if len(values) > 6 else 0),
                        )
                    )
            return pending

    def mark_fix_notification_sent(self, chat_id: int, message_id: int) -> bool:
        with self._lock:
            rows = self._read_rows()
            for row_number, values in enumerate(rows, start=2):
                saved_chat_id = str(values[5]) if len(values) > 5 else ""
                saved_message_id = str(values[6]) if len(values) > 6 else ""
                if saved_chat_id == str(chat_id) and saved_message_id == str(message_id):
                    (
                        self._service.spreadsheets()
                        .values()
                        .update(
                            spreadsheetId=self._spreadsheet_id,
                            range=f"'{self._quoted_sheet_name()}'!I{row_number}",
                            valueInputOption="RAW",
                            body={"values": [[True]]},
                        )
                        .execute()
                    )
                    return True
            return False

    def delete_fixed_rows(self) -> list[int]:
        with self._lock:
            rows = self._read_rows()
            checked: list[tuple[int, int]] = []
            for sheet_row, values in enumerate(rows, start=2):
                fixed_value = values[4] if len(values) > 4 else False
                if is_checked(fixed_value):
                    issue_id = self._safe_int(values[9] if len(values) > 9 else 0)
                    checked.append((sheet_row, issue_id))

            if not checked:
                return []

            requests = [
                {
                    "deleteDimension": {
                        "range": {
                            "sheetId": self._sheet_id,
                            "dimension": "ROWS",
                            "startIndex": row_number - 1,
                            "endIndex": row_number,
                        }
                    }
                }
                for row_number, _ in sorted(checked, reverse=True)
            ]
            (
                self._service.spreadsheets()
                .batchUpdate(
                    spreadsheetId=self._spreadsheet_id,
                    body={"requests": requests},
                )
                .execute()
            )
            return [issue_id for _, issue_id in checked]

    def _ensure_layout(self) -> None:
        header_range = f"'{self._quoted_sheet_name()}'!A1:K1"
        result = (
            self._service.spreadsheets()
            .values()
            .get(
                spreadsheetId=self._spreadsheet_id,
                range=header_range,
                valueRenderOption="FORMULA",
            )
            .execute()
        )
        current = result.get("values", [])
        current_headers = [str(value) for value in current[0]] if current else []

        if not any(value.strip() for value in current_headers):
            self._write_headers()
        elif current_headers[: len(HEADERS)] == HEADERS:
            pass
        elif current_headers[: len(LEGACY_HEADERS)] == LEGACY_HEADERS:
            self._migrate_legacy_layout()
        else:
            raise RuntimeError(
                "Worksheet headers are not recognized. Expected the bot layout in row 1."
            )

        requests = [
            {
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": self._sheet_id,
                        "gridProperties": {"frozenRowCount": 1},
                    },
                    "fields": "gridProperties.frozenRowCount",
                }
            },
            {
                "repeatCell": {
                    "range": {
                        "sheetId": self._sheet_id,
                        "startRowIndex": 0,
                        "endRowIndex": 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": 11,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": {"red": 0.12, "green": 0.31, "blue": 0.47},
                            "textFormat": {
                                "bold": True,
                                "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                            },
                            "horizontalAlignment": "CENTER",
                            "wrapStrategy": "WRAP",
                        }
                    },
                    "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,wrapStrategy)",
                }
            },
            self._column_width_request(0, 180),
            self._column_width_request(1, 240),
            self._column_width_request(2, 420),
            self._column_width_request(3, 180),
            self._column_width_request(4, 120),
            {
                "setDataValidation": {
                    "range": {
                        "sheetId": self._sheet_id,
                        "startRowIndex": 1,
                        "startColumnIndex": 3,
                        "endColumnIndex": 5,
                    },
                    "rule": {
                        "condition": {"type": "BOOLEAN"},
                        "strict": True,
                        "showCustomUi": True,
                    },
                }
            },
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": self._sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": 5,
                        "endIndex": 11,
                    },
                    "properties": {"hiddenByUser": True},
                    "fields": "hiddenByUser",
                }
            },
        ]
        (
            self._service.spreadsheets()
            .batchUpdate(
                spreadsheetId=self._spreadsheet_id,
                body={"requests": requests},
            )
            .execute()
        )

    def _write_headers(self) -> None:
        (
            self._service.spreadsheets()
            .values()
            .update(
                spreadsheetId=self._spreadsheet_id,
                range=f"'{self._quoted_sheet_name()}'!A1:K1",
                valueInputOption="RAW",
                body={"values": [HEADERS]},
            )
            .execute()
        )

    def _migrate_legacy_layout(self) -> None:
        response = (
            self._service.spreadsheets()
            .values()
            .get(
                spreadsheetId=self._spreadsheet_id,
                range=f"'{self._quoted_sheet_name()}'!A2:H",
                valueRenderOption="FORMULA",
            )
            .execute()
        )
        migrated = [self._migrate_legacy_row(row) for row in response.get("values", [])]
        (
            self._service.spreadsheets()
            .values()
            .clear(
                spreadsheetId=self._spreadsheet_id,
                range=f"'{self._quoted_sheet_name()}'!A:K",
                body={},
            )
            .execute()
        )
        values = [HEADERS, *migrated]
        (
            self._service.spreadsheets()
            .values()
            .update(
                spreadsheetId=self._spreadsheet_id,
                range=f"'{self._quoted_sheet_name()}'!A1:K{len(values)}",
                valueInputOption="USER_ENTERED",
                body={"values": values},
            )
            .execute()
        )
        for row_number in range(2, len(values) + 1):
            self._format_new_row(row_number)

    @classmethod
    def _migrate_legacy_row(cls, row: list[Any]) -> list[Any]:
        issue_id = cls._safe_int(row[0] if row else 0)
        old_photo = str(row[1]) if len(row) > 1 else ""
        message_url = cls._extract_hyperlink_url(old_photo)
        message_link = cls._link_formula(message_url) if message_url else old_photo
        return [
            message_link,
            "",
            str(row[2]) if len(row) > 2 else "",
            False,
            row[3] if len(row) > 3 else False,
            str(row[5]) if len(row) > 5 else "",
            str(row[6]) if len(row) > 6 else "",
            str(row[7]) if len(row) > 7 else "",
            False,
            issue_id,
            message_url,
        ]

    def _format_new_row(self, row_number: int) -> None:
        start_row = row_number - 1
        requests = [
            {
                "setDataValidation": {
                    "range": {
                        "sheetId": self._sheet_id,
                        "startRowIndex": start_row,
                        "endRowIndex": start_row + 1,
                        "startColumnIndex": 3,
                        "endColumnIndex": 5,
                    },
                    "rule": {
                        "condition": {"type": "BOOLEAN"},
                        "strict": True,
                        "showCustomUi": True,
                    },
                }
            },
            {
                "repeatCell": {
                    "range": {
                        "sheetId": self._sheet_id,
                        "startRowIndex": start_row,
                        "endRowIndex": start_row + 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": 5,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "verticalAlignment": "MIDDLE",
                            "wrapStrategy": "WRAP",
                        }
                    },
                    "fields": "userEnteredFormat(verticalAlignment,wrapStrategy)",
                }
            },
        ]
        (
            self._service.spreadsheets()
            .batchUpdate(
                spreadsheetId=self._spreadsheet_id,
                body={"requests": requests},
            )
            .execute()
        )

    def _read_rows(self) -> list[list[Any]]:
        response = (
            self._service.spreadsheets()
            .values()
            .get(
                spreadsheetId=self._spreadsheet_id,
                range=f"'{self._quoted_sheet_name()}'!A2:K",
                valueRenderOption="UNFORMATTED_VALUE",
            )
            .execute()
        )
        return response.get("values", [])

    @staticmethod
    def _is_duplicate(rows: list[list[Any]], chat_id: int, message_id: int) -> bool:
        for row in rows:
            saved_chat_id = str(row[5]) if len(row) > 5 else ""
            saved_message_id = str(row[6]) if len(row) > 6 else ""
            if saved_chat_id == str(chat_id) and saved_message_id == str(message_id):
                return True
        return False

    @classmethod
    def _next_issue_id(cls, rows: list[list[Any]]) -> int:
        ids = [cls._safe_int(row[9]) for row in rows if len(row) > 9]
        return max(ids, default=0) + 1

    @staticmethod
    def _safe_int(value: object) -> int:
        try:
            return int(float(str(value)))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _parse_row_number(updated_range: str) -> int:
        match = re.search(r"![A-Z]+(\d+):", updated_range)
        if not match:
            raise RuntimeError(f"Could not parse appended row from {updated_range!r}")
        return int(match.group(1))

    @staticmethod
    def _safe_user_text(value: str) -> str:
        return f"'{value}" if value.startswith(("=", "+", "-", "@")) else value

    @staticmethod
    def _link_formula(url: str) -> str:
        escaped = url.replace('"', '""')
        return f'=HYPERLINK("{escaped}","Открыть сообщение")'

    @staticmethod
    def _extract_hyperlink_url(value: str) -> str:
        match = re.search(r'HYPERLINK\("([^"]+)"', value, re.IGNORECASE)
        return match.group(1) if match else ""

    def _column_width_request(self, column_index: int, pixel_size: int) -> dict[str, Any]:
        return {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": self._sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": column_index,
                    "endIndex": column_index + 1,
                },
                "properties": {"pixelSize": pixel_size},
                "fields": "pixelSize",
            }
        }

    def _quoted_sheet_name(self) -> str:
        return self._sheet_name.replace("'", "''")


class DuplicateMessageError(RuntimeError):
    def __init__(self, chat_id: int, message_id: int) -> None:
        super().__init__(f"Message {chat_id}/{message_id} is already recorded")
