"""Exchange publishes more than mail over IMAP.

Alongside the mail folders, Exchange lists the mailbox's calendar, contacts,
journal, notes and tasks as if they were folders of messages. Fetching them
works -- a holiday in the calendar comes back as a text/plain message "From:
Microsoft Exchange Server" with no Message-ID -- which is exactly the problem:
indexed as mail, a calendar fills search with entries that are not mail, and
nothing in the protocol says which folders those are. They carry no
special-use attribute and no folder class is visible over IMAP.

What *is* visible is their name, and that is localised: a German mailbox says
Kalender, Kontakte, Aufgaben. Matching names one by one would drop a legitimate
folder called "Agenda" from a German mailbox, which is the worse mistake -- mail
that silently never gets indexed -- so a language is only recognised when its
whole set of default names is present at the top level, and only that
language's names are then excluded. A mailbox this cannot recognise is left
alone, with a warning, rather than guessed at; ``EMAILSERVER_EXCLUDED_SYNC_FOLDERS``
is the remedy for those.
"""

import base64
import logging
import re
import unicodedata

from src.config import settings

logger = logging.getLogger(__name__)

# The five default folders that hold something other than mail, per mailbox
# language: calendar, contacts, journal, notes, tasks. Spellings are Outlook's.
NON_MAIL_FOLDERS_BY_LANGUAGE: dict[str, tuple[str, ...]] = {
    "en": ("Calendar", "Contacts", "Journal", "Notes", "Tasks"),
    "de": ("Kalender", "Kontakte", "Journal", "Notizen", "Aufgaben"),
    "fr": ("Calendrier", "Contacts", "Journal", "Notes", "Tâches"),
    "es": ("Calendario", "Contactos", "Diario", "Notas", "Tareas"),
    "it": ("Calendario", "Contatti", "Diario", "Note", "Attività"),
    "nl": ("Agenda", "Contactpersonen", "Dagboek", "Notities", "Taken"),
    "pt-br": ("Calendário", "Contatos", "Diário", "Anotações", "Tarefas"),
    "pt-pt": ("Calendário", "Contactos", "Diário", "Notas", "Tarefas"),
}

# One spelling may differ from what is listed here, and Journal is the same in
# several languages, so four of five identifies a language without needing
# every guess to be right.
_MINIMUM_DEFAULTS_PRESENT = 4

_warned_accounts: set[int | None] = set()


def decode_imap_utf7(name: str) -> str:
    """A folder name as IMAP sends it (modified UTF-7, RFC 3501 5.1.3), in readable form.

    Exchange lists "Attività" as ``Attivit&AOA-``. The rest of this server keeps
    the raw form, which is what IMAP commands need, so this is only for
    comparing names. A name that does not decode is compared as it came.
    """

    def shifted(match: re.Match) -> str:
        encoded = match.group(1)
        if not encoded:
            return "&"
        try:
            return base64.b64decode(encoded.replace(",", "/") + "=" * (-len(encoded) % 4), validate=True).decode("utf-16-be")
        except (ValueError, UnicodeError):
            return match.group(0)

    return re.sub(r"&([^-]*)-", shifted, name)


def _root(name: str) -> str:
    top = decode_imap_utf7(name).split("/", 1)[0]
    return unicodedata.normalize("NFC", top).casefold()


def detect_non_mail_roots(folder_names: list[str]) -> frozenset[str]:
    """The top-level non-mail folders this mailbox has, or none if its language is unknown."""
    roots = {_root(name) for name in folder_names}
    best: frozenset[str] = frozenset()
    for defaults in NON_MAIL_FOLDERS_BY_LANGUAGE.values():
        present = frozenset({_root(default) for default in defaults} & roots)
        if len(present) >= _MINIMUM_DEFAULTS_PRESENT and len(present) > len(best):
            best = present
    return best


def exclude_non_mail_folders(config, folder_names: list[str]) -> list[str]:
    """The folders worth indexing as mail for this account."""
    excluded = {name.strip().casefold() for name in settings.excluded_sync_folders if name.strip()}
    if getattr(config, "provider", None) == "microsoft":
        detected = detect_non_mail_roots(folder_names)
        if not detected:
            account_id = getattr(config, "id", None)
            if account_id not in _warned_accounts:
                _warned_accounts.add(account_id)
                logger.warning(
                    "Account %s: could not tell which of Exchange's folders hold calendar, contacts or "
                    "tasks rather than mail, so all of them will be indexed. Name the ones to skip in "
                    "EMAILSERVER_EXCLUDED_SYNC_FOLDERS.",
                    account_id,
                )
        excluded |= detected
    if not excluded:
        return list(folder_names)
    return [name for name in folder_names if _root(name) not in excluded]
