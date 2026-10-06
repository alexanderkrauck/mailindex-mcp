"""Write flags back to an IMAP mailbox.

Kept apart from SMTPClient, which is a read path: everything here mutates a
mailbox the user owns, so it fails loudly rather than falling back.
"""

import contextlib
import logging
import re

logger = logging.getLogger(__name__)

# One response line, carrying both the UID and the flags that resulted.
UID_AND_FLAGS = re.compile(rb"\bUID\s+(\d+)\b.*?\bFLAGS\s+\(([^)]*)\)", re.DOTALL)
FLAGS_AND_UID = re.compile(rb"\bFLAGS\s+\(([^)]*)\).*?\bUID\s+(\d+)\b", re.DOTALL)


# [COPYUID <uidvalidity> <source set> <destination set>], RFC 4315. Without it a
# move cannot report where the message landed.
COPYUID = re.compile(rb"\[COPYUID\s+(\d+)\s+([\d,:]+)\s+([\d,:]+)\]", re.IGNORECASE)

# IMAP special-use attributes, RFC 6154. A mailbox that names its own Trash is
# far more reliable than guessing from the folder's name.
SPECIAL_USE = ("\\all", "\\archive", "\\drafts", "\\flagged", "\\junk", "\\sent", "\\trash")


def uid_chunks(uids: list[int]) -> list[list[int]]:
    """Split a UID list into sequence sets a server will actually accept.

    Many servers cap a command line at a few kilobytes, and 4,000 arbitrary UIDs
    is roughly 30 KB. Chunking here rather than in the caller means a bulk
    operation stays one connection and one lease, not one call per chunk.
    """
    from src.config import settings

    size = max(1, settings.imap_uid_set_chunk)
    return [uids[start : start + size] for start in range(0, len(uids), size)]


class ImapWriteError(RuntimeError):
    """The server refused, or could not be shown to have applied, a write."""


def parse_store_response(lines) -> dict[int, str]:
    """Pair each UID with the flags reported for that same UID.

    Not SMTPClient._extract_flags: that returns the first FLAGS across every
    line, so a batched STORE would write one message's resulting flags onto
    every message in the batch.
    """
    result: dict[int, str] = {}
    for line in lines:
        if not isinstance(line, (bytes, bytearray)):
            continue
        raw = bytes(line)
        match = UID_AND_FLAGS.search(raw)
        if match:
            result[int(match.group(1))] = match.group(2).decode("ascii", errors="ignore")
            continue
        # RFC 3501 says a UID FETCH response SHOULD carry UID, not MUST, and the
        # order of data items is not fixed.
        match = FLAGS_AND_UID.search(raw)
        if match:
            result[int(match.group(2))] = match.group(1).decode("ascii", errors="ignore")
    return result


async def store_flags(client, folder: str, uids: list[int], *, add: list[str], remove: list[str]) -> dict[int, str]:
    """Add and/or remove flags on messages in one folder.

    Returns the flags the server reports afterwards, per UID. A UID the server
    did not confirm is absent from the result rather than assumed applied.
    """
    if not uids or (not add and not remove):
        return {}

    selected = await client.client.select(f'"{folder}"')
    if selected.result != "OK":
        raise ImapWriteError(f"cannot select {folder}")

    confirmed: dict[int, str] = {}
    for chunk in uid_chunks(uids):
        sequence = ",".join(str(uid) for uid in chunk)
        for operation, flags in (("+FLAGS", add), ("-FLAGS", remove)):
            if not flags:
                continue
            # Not .SILENT: the untagged FETCH it suppresses is the only evidence
            # the write landed.
            response = await client.client.uid("store", sequence, operation, f"({' '.join(flags)})")
            if response.result != "OK":
                raise ImapWriteError(f"{operation} failed in {folder}: {response.result}")
            confirmed.update(parse_store_response(response.lines))

    missing = set(uids) - set(confirmed)
    if missing:
        # Some servers answer a STORE without untagged FETCH data. Ask directly
        # rather than reporting a state nothing confirmed.
        response = await client.client.uid("fetch", ",".join(str(uid) for uid in sorted(missing)), "(UID FLAGS)")
        if response.result == "OK":
            confirmed.update(parse_store_response(response.lines))
    return confirmed


def expand_uid_set(value: str) -> list[int]:
    """Expand an IMAP sequence set such as "3,7:9" into [3, 7, 8, 9]."""
    uids: list[int] = []
    for part in value.split(","):
        if ":" in part:
            low, _, high = part.partition(":")
            if low.isdigit() and high.isdigit():
                start, end = sorted((int(low), int(high)))
                uids.extend(range(start, end + 1))
        elif part.isdigit():
            uids.append(int(part))
    return uids


def parse_copyuid(lines) -> tuple[int | None, dict[int, int]]:
    """The destination UIDVALIDITY and each source UID's new UID.

    RFC 4315 guarantees the two sets correspond position by position, so this has
    to zip them rather than take the first: a batched move of twenty messages
    would otherwise record one destination UID for all twenty.

    The validity is the first field of COPYUID and belongs to the *destination*
    folder. Discarding it and reusing the source folder's would stamp a UID from
    one numbering space with the identifier of another, which is indistinguishable
    from the folder having been renumbered.
    """
    for line in lines:
        if not isinstance(line, (bytes, bytearray)):
            continue
        match = COPYUID.search(bytes(line))
        if match:
            validity = int(match.group(1))
            source = expand_uid_set(match.group(2).decode("ascii", errors="ignore"))
            destination = expand_uid_set(match.group(3).decode("ascii", errors="ignore"))
            return validity, dict(zip(source, destination, strict=False))
    return None, {}


async def move_messages(
    client, source: str, uids: list[int], destination: str
) -> dict[int, tuple[int, int | None]]:
    """Move messages between folders in one command. Returns source UID -> new UID.

    Prefers UID MOVE (RFC 6851). The COPY/STORE fallback deliberately does not
    EXPUNGE unless the server supports UID EXPUNGE: a bare EXPUNGE removes every
    \\Deleted message in the folder, including ones this call never touched.
    """
    if not uids:
        return {}
    selected = await client.client.select(f'"{source}"')
    if selected.result != "OK":
        raise ImapWriteError(f"cannot select {source}")

    capabilities = {item.upper() for item in getattr(client.client.protocol, "capabilities", set())}
    landed: dict[int, tuple[int, int | None]] = {}

    def record(response) -> None:
        validity, mapping = parse_copyuid(response.lines)
        landed.update({source_uid: (new_uid, validity) for source_uid, new_uid in mapping.items()})

    for chunk in uid_chunks(uids):
        sequence = ",".join(str(uid) for uid in chunk)
        if "MOVE" in capabilities:
            response = await client.client.uid("move", sequence, f'"{destination}"')
            if response.result != "OK":
                raise ImapWriteError(f"MOVE to {destination} failed: {response.result}")
            record(response)
            continue

        response = await client.client.uid("copy", sequence, f'"{destination}"')
        if response.result != "OK":
            raise ImapWriteError(f"COPY to {destination} failed: {response.result}")
        record(response)

        marked = await client.client.uid("store", sequence, "+FLAGS", "(\\Deleted)")
        if marked.result != "OK":
            raise ImapWriteError(f"could not mark the originals deleted in {source}")
        if "UIDPLUS" in capabilities:
            await client.client.uid("expunge", sequence)
        else:
            logger.info(
                "No UIDPLUS on this server; leaving %d original(s) in %s flagged "
                "\\Deleted rather than expunging the whole folder",
                len(chunk),
                source,
            )
    return landed


async def create_folder(client, name: str) -> bool:
    """Create a folder and subscribe to it. False if it already existed."""
    existing = {folder["name"] for folder in await list_folders(client)}
    if name in existing:
        return False
    response = await client.client.create(f'"{name}"')
    if response.result != "OK":
        detail = b" ".join(bytes(line) for line in response.lines if isinstance(line, (bytes, bytearray)))
        raise ImapWriteError(f"CREATE {name} failed: {detail.decode('ascii', errors='ignore')}")
    # Without SUBSCRIBE the folder exists but most clients will not show it.
    await client.client.subscribe(f'"{name}"')
    return True


async def append_message(client, folder: str, raw: bytes, *, flags: list[str]) -> int | None:
    """Append a message to a folder, e.g. a draft. Returns its UID when reported."""
    response = await client.client.append(
        raw, mailbox=f'"{folder}"', flags=f"({' '.join(flags)})" if flags else None
    )
    if response.result != "OK":
        raise ImapWriteError(f"APPEND to {folder} failed: {response.result}")
    # APPENDUID has the same shape as COPYUID, minus the source set.
    for line in response.lines:
        if isinstance(line, (bytes, bytearray)):
            match = re.search(rb"\[APPENDUID\s+\d+\s+(\d+)\]", bytes(line), re.IGNORECASE)
            if match:
                return int(match.group(1))
    return None


async def list_folders(client) -> list[dict]:
    """Every selectable folder, with its special-use role where the server names one."""
    response = await client.client.list('""', "*")
    if response.result != "OK":
        raise ImapWriteError("cannot list folders")
    folders = []
    for line in response.lines:
        parsed = client._parse_list_response(line)
        if not parsed:
            continue
        flags, name = parsed
        if "\\noselect" in flags:
            continue
        role = next((flag.lstrip("\\") for flag in flags if flag in SPECIAL_USE), None)
        folders.append({"name": name, "special_use": role})
    # The same folders the synchronizer indexes: offering a calendar as a place
    # to move mail to would only be a way to lose it.
    from src.email.exchange_folders import exclude_non_mail_folders

    kept = set(exclude_non_mail_folders(getattr(client, "config", None), [f["name"] for f in folders]))
    return [folder for folder in folders if folder["name"] in kept]


async def folder_message_count(client, name: str) -> int:
    """How many messages the server itself reports in a folder."""
    selected = await client.client.select(f'"{name}"')
    if selected.result != "OK":
        raise ImapWriteError(f"cannot select {name}")
    search = await client.search("ALL")
    if search.result != "OK":
        raise ImapWriteError(f"cannot count messages in {name}")
    return len(
        [
            token
            for line in search.lines
            if isinstance(line, (bytes, bytearray))
            for token in bytes(line).decode("ascii", errors="ignore").split()
            if token.isdigit()
        ]
    )


async def delete_folder(client, name: str) -> None:
    """Remove a folder. The caller is responsible for emptying it first."""
    # A folder cannot be deleted while it is selected on some servers.
    with contextlib.suppress(Exception):
        await client.client.close()
    response = await client.client.delete(f'"{name}"')
    if response.result != "OK":
        detail = b" ".join(
            bytes(line) for line in response.lines if isinstance(line, (bytes, bytearray))
        )
        raise ImapWriteError(f"DELETE {name} failed: {detail.decode('ascii', errors='ignore')}")


async def rename_folder(client, name: str, new_name: str) -> None:
    """Rename a folder, which also moves its children with it."""
    with contextlib.suppress(Exception):
        await client.client.close()
    response = await client.client.rename(f'"{name}"', f'"{new_name}"')
    if response.result != "OK":
        detail = b" ".join(
            bytes(line) for line in response.lines if isinstance(line, (bytes, bytearray))
        )
        raise ImapWriteError(f"RENAME {name} failed: {detail.decode('ascii', errors='ignore')}")
    await client.client.subscribe(f'"{new_name}"')


def child_folders(folders: list[dict], name: str) -> list[str]:
    """Folders nested under this one, by either hierarchy delimiter."""
    return [
        folder["name"]
        for folder in folders
        if folder["name"] != name
        and (folder["name"].startswith(f"{name}/") or folder["name"].startswith(f"{name}."))
    ]
