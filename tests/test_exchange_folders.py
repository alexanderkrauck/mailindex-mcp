"""Exchange lists its calendar, contacts and tasks as mail folders; they must not be indexed as mail."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.config import settings
from src.email import exchange_folders
from src.email.exchange_folders import decode_imap_utf7, detect_non_mail_roots, exclude_non_mail_folders

# What outlook.office365.com returned for a real English mailbox.
ENGLISH_MAILBOX = [
    "Archive",
    "Calendar",
    "Calendar/Birthdays",
    "Calendar/United States holidays",
    "Contacts",
    "Conversation History",
    "Deleted Items",
    "Drafts",
    "INBOX",
    "Journal",
    "Junk Email",
    "Notes",
    "Outbox",
    "Sent Items",
    "Tasks",
]
ENGLISH_MAIL = [
    "Archive",
    "Conversation History",
    "Deleted Items",
    "Drafts",
    "INBOX",
    "Junk Email",
    "Outbox",
    "Sent Items",
]

MICROSOFT = SimpleNamespace(id=11, provider="microsoft")


@pytest.fixture(autouse=True)
def quiet_state(monkeypatch):
    monkeypatch.setattr(settings, "excluded_sync_folders", [])
    monkeypatch.setattr(exchange_folders, "_warned_accounts", set())


def test_calendar_contacts_and_tasks_are_not_indexed_as_mail():
    assert exclude_non_mail_folders(MICROSOFT, ENGLISH_MAILBOX) == ENGLISH_MAIL


def test_a_german_mailbox_is_recognised_by_its_own_names():
    german = ["INBOX", "Gesendete Elemente", "Kalender", "Kalender/Geburtstage", "Kontakte", "Aufgaben", "Notizen", "Journal"]

    assert exclude_non_mail_folders(MICROSOFT, german) == ["INBOX", "Gesendete Elemente"]


def test_a_folder_that_merely_shares_a_name_in_another_language_is_mail():
    """'Agenda' is Dutch for calendar, and a perfectly good German folder for meeting agendas."""
    german = ["INBOX", "Agenda", "Kalender", "Kontakte", "Aufgaben", "Notizen", "Journal"]

    assert "Agenda" in exclude_non_mail_folders(MICROSOFT, german)


def test_a_users_own_nested_folder_with_a_default_name_is_kept():
    mailbox = [*ENGLISH_MAILBOX, "INBOX/Notes", "Projects/Calendar", "Projects"]

    kept = exclude_non_mail_folders(MICROSOFT, mailbox)

    assert "INBOX/Notes" in kept
    assert "Projects/Calendar" in kept
    assert "Notes" not in kept


def test_a_mailbox_in_an_unrecognised_language_is_left_alone_and_says_so(caplog):
    unknown = ["INBOX", "Postausgang", "Kalendarz", "Kontakty", "Zadania", "Notatki", "Dziennik"]

    with caplog.at_level("WARNING", logger="src.email.exchange_folders"):
        assert exclude_non_mail_folders(MICROSOFT, unknown) == unknown
        assert exclude_non_mail_folders(MICROSOFT, unknown) == unknown

    # Dropping a folder on a guess would mean mail that is never indexed, and
    # nobody would know. Indexing too much is visible and fixable.
    warnings = [record for record in caplog.records if "EMAILSERVER_EXCLUDED_SYNC_FOLDERS" in record.getMessage()]
    assert len(warnings) == 1, "warn once per account, not on every sync pass"


def test_an_ordinary_imap_account_keeps_a_folder_called_calendar():
    imap = SimpleNamespace(id=2, provider="imap")

    assert exclude_non_mail_folders(imap, ENGLISH_MAILBOX) == ENGLISH_MAILBOX


def test_the_operator_can_name_folders_to_skip_on_any_account(monkeypatch):
    monkeypatch.setattr(settings, "excluded_sync_folders", ["kalendarz", " Dziennik "])
    unknown = ["INBOX", "Kalendarz", "Kalendarz/Urodziny", "Dziennik", "Notatki"]

    assert exclude_non_mail_folders(MICROSOFT, unknown) == ["INBOX", "Notatki"]
    assert exclude_non_mail_folders(SimpleNamespace(id=2, provider="imap"), unknown) == ["INBOX", "Notatki"]


def imap_utf7(name: str) -> str:
    """The wire form of a folder name, so the cases below are written the way people read them."""
    import base64
    import re

    def encode(match):
        chunk = base64.b64encode(match.group(0).encode("utf-16-be")).decode().rstrip("=").replace("/", ",")
        return f"&{chunk}-"

    return re.sub(r"[^\x20-\x7e]+", encode, name.replace("&", "&-"))


@pytest.mark.parametrize(
    "defaults",
    [
        ("Calendrier", "Contacts", "Journal", "Notes", "Tâches"),
        ("Calendario", "Contatti", "Diario", "Note", "Attività"),
        ("Calendário", "Contatos", "Diário", "Anotações", "Tarefas"),
    ],
)
def test_non_ascii_default_folders_are_recognised_from_their_wire_form(defaults):
    """Exchange lists "Attività" as `Attivit&AOA-`; matching the raw string would never find it."""
    mailbox = ["INBOX", "Projekte", *(imap_utf7(name) for name in defaults), f"{imap_utf7(defaults[0])}/Sub"]

    kept = exclude_non_mail_folders(MICROSOFT, mailbox)

    assert kept == ["INBOX", "Projekte"]
    assert any("&" in name for name in mailbox), "the cases must exercise the encoded form"


def test_the_wire_decoder_handles_the_forms_the_rfc_defines():
    assert decode_imap_utf7("Attivit&AOA-") == "Attività"
    assert decode_imap_utf7("Anota&AOcA9Q-es") == "Anotações"
    assert decode_imap_utf7("Q&-A") == "Q&A"
    assert decode_imap_utf7("INBOX/Plain") == "INBOX/Plain"
    # Malformed input is left as it came rather than raising during a sync.
    assert decode_imap_utf7("Broken&!!-name") == "Broken&!!-name"


def test_detection_needs_most_of_a_language_not_a_single_name():
    assert detect_non_mail_roots(["INBOX", "Notes", "Journal", "Contacts"]) == frozenset()
    assert detect_non_mail_roots(["INBOX", "Notes", "Journal", "Contacts", "Tasks"]) == frozenset(
        {"notes", "journal", "contacts", "tasks"}
    )


# ---- where it is applied --------------------------------------------------------------------------------


def listing(names):
    lines = [f'(\\HasNoChildren) "/" "{name}"'.encode() for name in names]
    lines.append(b"LIST completed.")
    return SimpleNamespace(result="OK", lines=lines)


@pytest.mark.asyncio
async def test_the_synchronizer_never_visits_non_mail_folders():
    from src.email.smtp_client import SMTPClient

    client = SMTPClient(
        SimpleNamespace(id=11, name="contoso.com", provider="microsoft", host="outlook.office365.com")
    )
    client.client = AsyncMock()
    client.client.list.return_value = listing(ENGLISH_MAILBOX)

    assert await client._get_folders() == ENGLISH_MAIL


@pytest.mark.asyncio
async def test_a_calendar_is_not_offered_as_somewhere_to_move_mail():
    from src.email.imap_writer import list_folders
    from src.email.smtp_client import SMTPClient

    client = SMTPClient(
        SimpleNamespace(id=11, name="contoso.com", provider="microsoft", host="outlook.office365.com")
    )
    client.client = AsyncMock()
    client.client.list.return_value = listing(ENGLISH_MAILBOX)

    folders = await list_folders(client)

    assert [folder["name"] for folder in folders] == ENGLISH_MAIL
