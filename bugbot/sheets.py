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
    "Описание бага",
    "Компетенция",
    "Подтверждение исправления",
    "Исправлено",
    "source_chat_id",
    "source_message_id",
    "dev_notification_sent",
    "fix_notification_sent",
    "issue_id",
]
PRE_ASSIGNMENT_HEADERS = [
    "Ссылка на сообщение",
    "Описание бага",
    "Компетенция",
    "Подтверждение исправления",
    "Исправлено",
    "source_chat_id",
    "source_message_id",
    "fix_notification_sent",
    "issue_id",
]
PRE_COMPETENCE_HEADERS = [
    "Ссылка на сообщение",
    "Описание бага",
    "Подтверждение исправления",
    "Исправлено",
    "source_chat_id",
    "source_message_id",
    "fix_notification_sent",
    "issue_id",
    "message_url",
]
PHOTO_HEADERS = [
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
    row_url: str


@dataclass(frozen=True)
class CompetenceNotification:
    issue_id: int
    description: str
    competence: str
    row_url: str
    source_chat_id: int
    source_message_id: int


@dataclass(frozen=True)
class FixNotification:
    issue_id: int
    description: str
    row_url: str
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
            column_count = int(
                properties.get("gridProperties", {}).get("columnCount", 0)
            )
            if column_count < len(HEADERS):
                (
                    self._service.spreadsheets()
                    .batchUpdate(
                        spreadsheetId=self._spreadsheet_id,
                        body={
                            "requests": [
                                {
                                    "updateSheetProperties": {
                                        "properties": {
                                            "sheetId": self._sheet_id,
                                            "gridProperties": {
                                                "columnCount": len(HEADERS)
                                            },
                                        },
                                        "fields": "gridProperties.columnCount",
                                    }
                                }
                            ]
                        },
                    )
                    .execute()
                )
            self._ensure_layout()

    def append_bug(
        self,
        description: str,
        message_url: str | None,
        chat_id: int,
        message_id: int,
    ) -> AppendResult:
        with self._lock:
            rows = self._read_rows()
            if self._is_duplicate(rows, chat_id, message_id):
                raise DuplicateMessageError(chat_id, message_id)

            issue_id = self._next_issue_id(rows)
            link_value = self._link_formula(message_url) if message_url else ""
            row_number = self._next_available_row(rows)
            (
                self._service.spreadsheets()
                .values()
                .update(
                    spreadsheetId=self._spreadsheet_id,
                    range=(
                        f"'{self._quoted_sheet_name()}'!"
                        f"A{row_number}:J{row_number}"
                    ),
                    valueInputOption="USER_ENTERED",
                    body={
                        "majorDimension": "ROWS",
                        "values": [[
                            link_value,
                            self._safe_user_text(description),
                            "",
                            False,
                            False,
                            str(chat_id),
                            str(message_id),
                            False,
                            False,
                            issue_id,
                        ]],
                    },
                )
                .execute()
            )
            self._format_new_row(row_number)
            return AppendResult(
                issue_id=issue_id,
                row_number=row_number,
                row_url=self._row_url(row_number),
            )

    def get_pending_competence_notifications(self) -> list[CompetenceNotification]:
        with self._lock:
            pending: list[CompetenceNotification] = []
            for row_number, values in enumerate(self._read_rows(), start=2):
                competence = str(values[2]).strip() if len(values) > 2 else ""
                already_sent = values[7] if len(values) > 7 else False
                if competence.casefold() not in {"фронт", "бэкенд"}:
                    continue
                if is_checked(already_sent):
                    continue
                pending.append(
                    CompetenceNotification(
                        issue_id=self._safe_int(values[9] if len(values) > 9 else 0),
                        description=str(values[1]) if len(values) > 1 else "",
                        competence=competence,
                        row_url=self._row_url(row_number),
                        source_chat_id=self._safe_int(values[5] if len(values) > 5 else 0),
                        source_message_id=self._safe_int(values[6] if len(values) > 6 else 0),
                    )
                )
            return pending

    def mark_competence_notification_sent(
        self,
        chat_id: int,
        message_id: int,
    ) -> bool:
        return self._mark_notification_sent(
            chat_id=chat_id,
            message_id=message_id,
            column="H",
        )

    def get_pending_fix_notifications(self) -> list[FixNotification]:
        with self._lock:
            pending: list[FixNotification] = []
            for row_number, values in enumerate(self._read_rows(), start=2):
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
                            description=str(values[1]) if len(values) > 1 else "",
                            row_url=self._row_url(row_number),
                            source_chat_id=self._safe_int(values[5] if len(values) > 5 else 0),
                            source_message_id=self._safe_int(values[6] if len(values) > 6 else 0),
                        )
                    )
            return pending

    def mark_fix_notification_sent(self, chat_id: int, message_id: int) -> bool:
        return self._mark_notification_sent(
            chat_id=chat_id,
            message_id=message_id,
            column="I",
        )

    def _mark_notification_sent(
        self,
        *,
        chat_id: int,
        message_id: int,
        column: str,
    ) -> bool:
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
                            range=(
                                f"'{self._quoted_sheet_name()}'!"
                                f"{column}{row_number}"
                            ),
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
        header_range = f"'{self._quoted_sheet_name()}'!A1:J1"
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
        elif current_headers[: len(PRE_ASSIGNMENT_HEADERS)] == PRE_ASSIGNMENT_HEADERS:
            self._migrate_pre_assignment_layout()
        elif current_headers[: len(PRE_COMPETENCE_HEADERS)] == PRE_COMPETENCE_HEADERS:
            self._migrate_pre_competence_layout()
        elif (
            len(current_headers) >= 9
            and current_headers == PHOTO_HEADERS[: len(current_headers)]
        ):
            self._migrate_photo_layout()
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
                        "endColumnIndex": 10,
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
            {
                "repeatCell": {
                    "range": {
                        "sheetId": self._sheet_id,
                        "startRowIndex": 1,
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
            self._column_width_request(0, 85),
            self._column_width_request(1, 180),
            self._column_width_request(2, 80),
            self._column_width_request(3, 105),
            self._column_width_request(4, 70),
            {
                "setDataValidation": {
                    "range": {
                        "sheetId": self._sheet_id,
                        "startRowIndex": 1,
                        "startColumnIndex": 2,
                        "endColumnIndex": 3,
                    },
                    "rule": {
                        "condition": {
                            "type": "ONE_OF_LIST",
                            "values": [
                                {"userEnteredValue": "Бэкенд"},
                                {"userEnteredValue": "Фронт"},
                            ],
                        },
                        "strict": True,
                        "showCustomUi": True,
                    },
                }
            },
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
                        "startIndex": 0,
                        "endIndex": 5,
                    },
                    "properties": {"hiddenByUser": False},
                    "fields": "hiddenByUser",
                }
            },
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": self._sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": 5,
                        "endIndex": 10,
                    },
                    "properties": {"hiddenByUser": True},
                    "fields": "hiddenByUser",
                }
            },
            {
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": self._sheet_id,
                        "gridProperties": {"columnCount": 10},
                    },
                    "fields": "gridProperties.columnCount",
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
                range=f"'{self._quoted_sheet_name()}'!A1:J1",
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
        migrated = [
            self._migrate_legacy_row(row)
            for row in response.get("values", [])
            if self._legacy_row_has_issue(row)
        ]
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
                range=f"'{self._quoted_sheet_name()}'!A1:J{len(values)}",
                valueInputOption="USER_ENTERED",
                body={"values": values},
            )
            .execute()
        )

    def _migrate_photo_layout(self) -> None:
        response = (
            self._service.spreadsheets()
            .values()
            .get(
                spreadsheetId=self._spreadsheet_id,
                range=f"'{self._quoted_sheet_name()}'!A2:K",
                valueRenderOption="FORMULA",
            )
            .execute()
        )
        migrated = [
            self._migrate_photo_row(row)
            for row in response.get("values", [])
            if self._photo_row_has_issue(row)
        ]
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
                range=f"'{self._quoted_sheet_name()}'!A1:J{len(values)}",
                valueInputOption="USER_ENTERED",
                body={"values": values},
            )
            .execute()
        )

    def _migrate_pre_assignment_layout(self) -> None:
        response = (
            self._service.spreadsheets()
            .values()
            .get(
                spreadsheetId=self._spreadsheet_id,
                range=f"'{self._quoted_sheet_name()}'!A2:J",
                valueRenderOption="FORMULA",
            )
            .execute()
        )
        migrated = [
            self._migrate_pre_assignment_row(row)
            for row in response.get("values", [])
            if self._pre_assignment_row_has_issue(row)
        ]
        (
            self._service.spreadsheets()
            .values()
            .clear(
                spreadsheetId=self._spreadsheet_id,
                range=f"'{self._quoted_sheet_name()}'!A:J",
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
                range=f"'{self._quoted_sheet_name()}'!A1:J{len(values)}",
                valueInputOption="USER_ENTERED",
                body={"values": values},
            )
            .execute()
        )

    def _migrate_pre_competence_layout(self) -> None:
        response = (
            self._service.spreadsheets()
            .values()
            .get(
                spreadsheetId=self._spreadsheet_id,
                range=f"'{self._quoted_sheet_name()}'!A2:J",
                valueRenderOption="FORMULA",
            )
            .execute()
        )
        migrated = [
            self._migrate_pre_competence_row(row)
            for row in response.get("values", [])
            if self._pre_competence_row_has_issue(row)
        ]
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
                range=f"'{self._quoted_sheet_name()}'!A1:J{len(values)}",
                valueInputOption="USER_ENTERED",
                body={"values": values},
            )
            .execute()
        )

    @staticmethod
    def _migrate_pre_assignment_row(row: list[Any]) -> list[Any]:
        return [
            row[0] if len(row) > 0 else "",
            row[1] if len(row) > 1 else "",
            row[2] if len(row) > 2 else "",
            row[3] if len(row) > 3 else False,
            row[4] if len(row) > 4 else False,
            row[5] if len(row) > 5 else "",
            row[6] if len(row) > 6 else "",
            False,
            row[7] if len(row) > 7 else False,
            row[8] if len(row) > 8 else 0,
        ]

    @staticmethod
    def _pre_assignment_row_has_issue(row: list[Any]) -> bool:
        fields = [
            row[0] if len(row) > 0 else "",
            row[1] if len(row) > 1 else "",
            row[5] if len(row) > 5 else "",
            row[6] if len(row) > 6 else "",
        ]
        issue_id = GoogleSheetStore._safe_int(row[8] if len(row) > 8 else 0)
        return issue_id > 0 or any(str(value).strip() for value in fields)

    @staticmethod
    def _migrate_pre_competence_row(row: list[Any]) -> list[Any]:
        return [
            row[0] if len(row) > 0 else "",
            row[1] if len(row) > 1 else "",
            "",
            row[2] if len(row) > 2 else False,
            row[3] if len(row) > 3 else False,
            row[4] if len(row) > 4 else "",
            row[5] if len(row) > 5 else "",
            False,
            row[6] if len(row) > 6 else False,
            row[7] if len(row) > 7 else 0,
        ]

    @staticmethod
    def _pre_competence_row_has_issue(row: list[Any]) -> bool:
        fields = [
            row[0] if len(row) > 0 else "",
            row[1] if len(row) > 1 else "",
            row[4] if len(row) > 4 else "",
            row[5] if len(row) > 5 else "",
        ]
        issue_id = GoogleSheetStore._safe_int(row[7] if len(row) > 7 else 0)
        return issue_id > 0 or any(str(value).strip() for value in fields)

    @staticmethod
    def _photo_row_has_issue(row: list[Any]) -> bool:
        fields = [
            row[0] if len(row) > 0 else "",
            row[2] if len(row) > 2 else "",
            row[5] if len(row) > 5 else "",
            row[6] if len(row) > 6 else "",
        ]
        issue_id = GoogleSheetStore._safe_int(row[9] if len(row) > 9 else 0)
        return issue_id > 0 or any(str(value).strip() for value in fields)

    @staticmethod
    def _legacy_row_has_issue(row: list[Any]) -> bool:
        fields = [
            row[0] if len(row) > 0 else "",
            row[2] if len(row) > 2 else "",
            row[5] if len(row) > 5 else "",
            row[6] if len(row) > 6 else "",
        ]
        return any(str(value).strip() for value in fields)

    @staticmethod
    def _migrate_photo_row(row: list[Any]) -> list[Any]:
        return [
            row[0] if len(row) > 0 else "",
            row[2] if len(row) > 2 else "",
            "",
            row[3] if len(row) > 3 else False,
            row[4] if len(row) > 4 else False,
            row[5] if len(row) > 5 else "",
            row[6] if len(row) > 6 else "",
            False,
            row[8] if len(row) > 8 else False,
            row[9] if len(row) > 9 else 0,
        ]

    @classmethod
    def _migrate_legacy_row(cls, row: list[Any]) -> list[Any]:
        issue_id = cls._safe_int(row[0] if row else 0)
        old_photo = str(row[1]) if len(row) > 1 else ""
        message_url = cls._extract_hyperlink_url(old_photo)
        message_link = cls._link_formula(message_url) if message_url else old_photo
        return [
            message_link,
            str(row[2]) if len(row) > 2 else "",
            "",
            False,
            row[3] if len(row) > 3 else False,
            str(row[5]) if len(row) > 5 else "",
            str(row[6]) if len(row) > 6 else "",
            False,
            False,
            issue_id,
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
                range=f"'{self._quoted_sheet_name()}'!A2:J",
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

    @classmethod
    def _next_available_row(cls, rows: list[list[Any]]) -> int:
        occupied = [
            sheet_row
            for sheet_row, row in enumerate(rows, start=2)
            if cls._new_row_has_issue(row)
        ]
        return max(occupied, default=1) + 1

    @staticmethod
    def _new_row_has_issue(row: list[Any]) -> bool:
        fields = [
            row[0] if len(row) > 0 else "",
            row[1] if len(row) > 1 else "",
            row[5] if len(row) > 5 else "",
            row[6] if len(row) > 6 else "",
        ]
        issue_id = GoogleSheetStore._safe_int(row[9] if len(row) > 9 else 0)
        return issue_id > 0 or any(str(value).strip() for value in fields)

    def _row_url(self, row_number: int) -> str:
        return (
            f"https://docs.google.com/spreadsheets/d/{self._spreadsheet_id}/edit"
            f"#gid={self._sheet_id}&range=A{row_number}:E{row_number}"
        )

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
