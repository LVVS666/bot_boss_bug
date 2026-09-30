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
        photo_url: str | None,
        chat_id: int,
        message_id: int,
        telegram_file_id: str,
    ) -> AppendResult:
        with self._lock:
            rows = self._read_rows()
            if self._is_duplicate(rows, chat_id, message_id):
                raise DuplicateMessageError(chat_id, message_id)

            issue_id = self._next_issue_id(rows)
            photo_value = (
                f'=HYPERLINK("{photo_url}","Открыть фото")'
                if photo_url
                else "Фото сохранено в Telegram"
            )
            response = (
                self._service.spreadsheets()
                .values()
                .append(
                    spreadsheetId=self._spreadsheet_id,
                    range=f"'{self._quoted_sheet_name()}'!A:H",
                    valueInputOption="USER_ENTERED",
                    insertDataOption="INSERT_ROWS",
                    includeValuesInResponse=True,
                    body={
                        "majorDimension": "ROWS",
                        "values": [[
                            issue_id,
                            photo_value,
                            description,
                            False,
                            "",
                            str(chat_id),
                            str(message_id),
                            telegram_file_id,
                        ]],
                    },
                )
                .execute()
            )
            updated_range = response["updates"]["updatedRange"]
            row_number = self._parse_row_number(updated_range)
            self._format_new_row(row_number)
            return AppendResult(issue_id=issue_id, row_number=row_number)

    def delete_checked_rows(self) -> list[int]:
        with self._lock:
            rows = self._read_rows()
            checked: list[tuple[int, int]] = []
            for sheet_row, values in enumerate(rows, start=2):
                delete_value = values[3] if len(values) > 3 else False
                if is_checked(delete_value):
                    issue_id = self._safe_int(values[0] if values else None)
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
        header_range = f"'{self._quoted_sheet_name()}'!A1:H1"
        result = (
            self._service.spreadsheets()
            .values()
            .get(spreadsheetId=self._spreadsheet_id, range=header_range)
            .execute()
        )
        current = result.get("values", [])
        if not current or not any(str(value).strip() for value in current[0]):
            (
                self._service.spreadsheets()
                .values()
                .update(
                    spreadsheetId=self._spreadsheet_id,
                    range=header_range,
                    valueInputOption="RAW",
                    body={"values": [HEADERS]},
                )
                .execute()
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
                        "endColumnIndex": 8,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": {"red": 0.12, "green": 0.31, "blue": 0.47},
                            "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
                            "horizontalAlignment": "CENTER",
                        }
                    },
                    "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)",
                }
            },
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": self._sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": 5,
                        "endIndex": 8,
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
                        "endColumnIndex": 4,
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
                range=f"'{self._quoted_sheet_name()}'!A2:H",
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
        ids = [cls._safe_int(row[0]) for row in rows if row]
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

    def _quoted_sheet_name(self) -> str:
        return self._sheet_name.replace("'", "''")


class DuplicateMessageError(RuntimeError):
    def __init__(self, chat_id: int, message_id: int) -> None:
        super().__init__(f"Message {chat_id}/{message_id} is already recorded")

