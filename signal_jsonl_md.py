#!/usr/bin/env python3

import base64
import hashlib
import json
import logging
import re
import shutil
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "hal"))
sys.path.insert(0, str(ROOT.parent / "message_md"))

import config
import markdown
import message
import message_md


@dataclass
class Recipient:
    id: str
    kind: str
    phone: str = ""
    aci: str = ""
    export_name: str = ""
    group_title: str = ""
    member_count: int = 0
    signal_group_key: str = ""
    member_recipient_ids: list = field(default_factory=list)


class RunStats:
    def __init__(self):
        self.chat_items_read = 0
        self.attachments_copied = 0
        self.attachments_missing = 0
        self.skipped = Counter()


def _string(value):
    return str(value) if value is not None else ""


def _group_key_from_master_key(master_key):
    if not isinstance(master_key, str) or not master_key:
        return ""
    try:
        decoded = base64.b64decode(master_key, validate=True)
    except (ValueError, base64.binascii.Error):
        return ""
    if len(decoded) != 32:
        return ""
    return hashlib.sha256(decoded).hexdigest()


def _export_name(contact):
    first = contact.get("profileGivenName", "")
    last = contact.get("profileFamilyName", "")
    return " ".join(part for part in (first, last) if isinstance(part, str) and part).strip()


def _group_title(group):
    title = group.get("title")
    snapshot = group.get("snapshot")
    if isinstance(snapshot, dict):
        snapshot_title = snapshot.get("title")
        if isinstance(snapshot_title, dict):
            title = snapshot_title.get("title") or title
        elif isinstance(snapshot_title, str):
            title = snapshot_title or title
    return _string(title).strip()


def _group_members(group):
    snapshot = group.get("snapshot")
    members = snapshot.get("members") if isinstance(snapshot, dict) else None
    if not isinstance(members, list):
        members = group.get("members", [])
    if not isinstance(members, list):
        return []
    member_ids = []
    for member in members:
        if isinstance(member, dict):
            member_id = member.get("userId")
        else:
            member_id = member
        if member_id is not None:
            member_ids.append(_string(member_id))
    return member_ids


def _safe_filename(name):
    return re.sub(r'[\\/:*?"<>|#^\[\]]', "-", name).strip()


def _iter_export_records(export_file, recipients, chats, config_obj, stats, list_mode=None):
    header_seen = False
    group_recipients = []

    with open(export_file, "r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue

            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                logging.warning("Skipping malformed JSON at line %d", line_number)
                stats.skipped["malformed JSON"] += 1
                continue

            if not isinstance(record, dict):
                stats.skipped["invalid record"] += 1
                continue

            record_keys = [key for key in record if key in {
                "account", "recipient", "chat", "stickerPack", "chatItem"
            }]
            if not record_keys:
                if header_seen:
                    for unknown_kind in record:
                        stats.skipped[f"unknown record: {unknown_kind}"] += 1
                    continue
                version = record.get("version")
                if version != "1":
                    raise ValueError(f"Unsupported or missing Signal export version: {version!r}")
                header_seen = True
                continue

            if not header_seen:
                raise ValueError("Signal export header is missing or invalid")
            if len(record_keys) != 1:
                stats.skipped["invalid record"] += 1
                continue

            record_kind = record_keys[0]
            payload = record[record_kind]

            if record_kind in ("account", "stickerPack"):
                continue

            if record_kind == "recipient":
                if not isinstance(payload, dict):
                    stats.skipped["invalid recipient"] += 1
                    continue
                recipient_id = _string(payload.get("id"))
                if not recipient_id:
                    stats.skipped["recipient without id"] += 1
                    continue

                if "self" in payload:
                    recipients[recipient_id] = Recipient(recipient_id, "self")
                elif isinstance(payload.get("contact"), dict):
                    contact = payload["contact"]
                    recipients[recipient_id] = Recipient(
                        id=recipient_id,
                        kind="contact",
                        phone=_string(contact.get("e164")),
                        aci=_string(contact.get("aci")),
                        export_name=_export_name(contact),
                    )
                elif isinstance(payload.get("group"), dict):
                    group = payload["group"]
                    member_ids = _group_members(group)
                    recipient = Recipient(
                        id=recipient_id,
                        kind="group",
                        group_title=_group_title(group),
                        member_count=len(member_ids),
                        signal_group_key=_group_key_from_master_key(group.get("masterKey")),
                        member_recipient_ids=member_ids,
                    )
                    recipients[recipient_id] = recipient
                    group_recipients.append(recipient)
                else:
                    stats.skipped["unknown recipient kind"] += 1
                continue

            if record_kind == "chat":
                if isinstance(payload, dict):
                    chat_id = _string(payload.get("id"))
                    recipient_id = _string(payload.get("recipientId"))
                    if chat_id:
                        chats[chat_id] = recipient_id
                else:
                    stats.skipped["invalid chat"] += 1
                continue

            stats.chat_items_read += 1
            if list_mode:
                continue
            if not isinstance(payload, dict):
                stats.skipped["invalid chatItem"] += 1
                continue

            yield payload

    if not header_seen:
        raise ValueError("Signal export header is missing or invalid")

    if list_mode == "groups":
        chat_recipient_ids = set(chats.values())
        for recipient in group_recipients:
            if recipient.id not in chat_recipient_ids:
                continue
            print(
                f"{recipient.group_title or '(untitled group)'}\t"
                f"{recipient.member_count}\t{recipient.signal_group_key}"
            )
    elif list_mode == "people":
        for recipient in recipients.values():
            if recipient.kind != "contact":
                continue
            if _lookup_person(recipient, config_obj, update_aci=False):
                continue
            print(f"{recipient.phone}\t{recipient.aci}\t{recipient.export_name}")


def _find_configured_person(recipient, config_obj):
    person = False
    if recipient.phone:
        person = config_obj.get_person_by_number(recipient.phone)
    if not person and recipient.aci:
        person = config_obj.get_person_by_signal_aci(recipient.aci)
    return person


def _ensure_person_first_name(person, fallback_name="", verbose=False):
    if person.first_name:
        return

    identity = getattr(person, "identity", None)
    name = _string(getattr(identity, "full_name", "")).strip() or _string(
        fallback_name
    ).strip()
    if name:
        person.first_name = name.split()[0]
        if verbose:
            logging.warning(
                "Signal contact %r has no configured first_name; using %r for "
                "message headers. Set first_name in people.json to avoid this fallback.",
                person.slug,
                person.first_name,
            )


def _suggest_group_slug(title, fingerprint, existing_slugs):
    base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    base = base or f"signal-{fingerprint[:8]}"
    slug = base
    suffix = 2
    while slug in existing_slugs:
        slug = f"{base}-{suffix}"
        suffix += 1
    return slug


def _configured_group_people(recipient, recipients, config_obj):
    members = []
    for member_id in recipient.member_recipient_ids or []:
        member = recipients.get(member_id)
        if not member:
            continue
        if member.kind == "self":
            person = config_obj.me
        elif member.kind == "contact":
            person = _find_configured_person(member, config_obj)
        else:
            person = False
        if person and person.slug not in members:
            members.append(person.slug)
    return members


def _new_group_config_entry(recipient, recipients, config_obj, existing_slugs):
    title = recipient.group_title or f"Signal group {recipient.signal_group_key[:8]}"
    slug = _suggest_group_slug(title, recipient.signal_group_key, existing_slugs)
    return {
        "id": slug,
        "slug": slug,
        "description": title,
        "people": _configured_group_people(recipient, recipients, config_obj),
        "signal-group-key": recipient.signal_group_key,
    }


def _find_group_config_entry(groups, description):
    return next(
        (
            group_entry
            for group_entry in groups
            if isinstance(group_entry, dict)
            and group_entry.get("description") == description
        ),
        None,
    )


def _add_group_to_config(recipient, recipients, config_obj):
    if not recipient.signal_group_key:
        logging.error(
            "Cannot add Signal group %r because its fingerprint could not be computed",
            recipient.group_title or "(untitled group)",
        )
        return ""

    groups_path = Path(config_obj.config_folder) / "groups.json"
    try:
        with groups_path.open("r", encoding="utf-8") as handle:
            groups_config = json.load(handle)
        groups = groups_config.get("groups")
        if not isinstance(groups, list):
            raise ValueError("groups.json must contain a 'groups' array")

        existing_group = _find_group_config_entry(
            groups, recipient.group_title
        ) if recipient.group_title else None
        if existing_group:
            existing_key = existing_group.get("signal-group-key", "")
            if existing_key and existing_key != recipient.signal_group_key:
                logging.error(
                    "The matching groups.json entry %r already has a different Signal group fingerprint",
                    existing_group.get("slug", ""),
                )
                return ""
            existing_group["signal-group-key"] = recipient.signal_group_key
            group_entry = existing_group
            added_new_group = False
        else:
            existing_slugs = {
                _string(group_entry.get("slug"))
                for group_entry in groups
                if isinstance(group_entry, dict)
            }
            group_entry = _new_group_config_entry(
                recipient, recipients, config_obj, existing_slugs
            )
            groups.append(group_entry)
            added_new_group = True

        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=groups_path.parent,
            prefix=groups_path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(groups_config, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        temp_path.replace(groups_path)
    except (OSError, ValueError, TypeError) as error:
        if "temp_path" in locals() and temp_path.exists():
            temp_path.unlink()
        logging.error("Could not add Signal group to %s: %s", groups_path, error)
        return ""

    configured_group = next(
        (
            group
            for group in config_obj.groups
            if group.slug == group_entry.get("slug")
        ),
        None,
    )
    if not configured_group:
        configured_group = config.group.Group()
        config_obj.groups.append(configured_group)
        configured_group.id = group_entry["id"]
        configured_group.slug = group_entry["slug"]
        configured_group.description = group_entry["description"]
        configured_group.members = group_entry.get("people", [])
    configured_group.signal_group_key = recipient.signal_group_key
    action = "Added" if added_new_group else "Updated fingerprint for"
    logging.info("%s Signal group %r in %s", action, group_entry["description"], groups_path)
    return group_entry["slug"]


def _lookup_person(
    recipient,
    config_obj,
    stats=None,
    warned=None,
    update_aci=True,
    verbose=False,
):
    person = _find_configured_person(recipient, config_obj)

    if person:
        _ensure_person_first_name(
            person,
            recipient.export_name or person.slug.replace("-", " "),
            verbose,
        )
        if update_aci and recipient.aci and not person.socials.signal_aci:
            person.socials.signal_aci = recipient.aci
            config_obj.save_people()
        return person

    if update_aci and config_obj.create_people:
        person = config_obj.get_or_create_person(
            full_name=recipient.export_name or "Signal contact",
            mobile=recipient.phone,
            source="Signal",
            prompt=False,
        )
        if person:
            person.first_name = person.identity.first_name
            person.last_name = person.identity.last_name
            if recipient.aci and not person.socials.signal_aci:
                person.socials.signal_aci = recipient.aci
                config_obj.save_people()
            return person

    if stats is not None and warned is not None and recipient.id not in warned:
        logging.warning("Skipping messages for a Signal contact not found in people.json")
        warned.add(recipient.id)
    return False


def _find_source_attachment(export_folder, attachment_hash, content_hash_index=None):
    source_root = Path(export_folder) / "files"
    source_folder = source_root / attachment_hash[:2]
    matches = sorted(source_folder.glob(attachment_hash + ".*"))
    if matches:
        return matches[0]

    if content_hash_index is None:
        content_hash_index = {}
    index_key = str(source_root.resolve())
    if index_key not in content_hash_index:
        content_hash_index[index_key] = {}
        if source_root.is_dir():
            for source_path in sorted(source_root.rglob("*")):
                if source_path.is_file():
                    digest = _file_hash(source_path)
                    content_hash_index[index_key].setdefault(digest, source_path)
    return content_hash_index[index_key].get(attachment_hash)


def _parse_attachment(
    raw_attachment,
    timestamp,
    export_folder,
    stats,
    same_message_index,
    content_hash_index=None,
):
    if not isinstance(raw_attachment, dict):
        stats.attachments_missing += 1
        logging.warning("Skipping an invalid Signal attachment record")
        return None

    pointer = raw_attachment.get("pointer")
    if not isinstance(pointer, dict):
        stats.attachments_missing += 1
        logging.warning("Skipping a Signal attachment without a pointer")
        return None

    if raw_attachment.get("wasDownloaded", pointer.get("wasDownloaded", True)) is False:
        stats.attachments_missing += 1
        logging.warning("Signal attachment was not downloaded; omitting its embed")
        return None

    locator = pointer.get("locatorInfo")
    encoded_hash = locator.get("plaintextHash") if isinstance(locator, dict) else None
    if not isinstance(encoded_hash, str):
        stats.attachments_missing += 1
        logging.warning("Signal attachment has no usable content hash; omitting its embed")
        return None

    try:
        digest = base64.b64decode(encoded_hash, validate=True)
    except (ValueError, base64.binascii.Error):
        digest = b""
    if len(digest) != 32:
        stats.attachments_missing += 1
        logging.warning("Signal attachment has an invalid content hash; omitting its embed")
        return None

    attachment_hash = digest.hex()
    source_path = _find_source_attachment(
        export_folder, attachment_hash, content_hash_index
    )
    if source_path is None:
        stats.attachments_missing += 1
        logging.warning("Signal attachment file is missing; omitting its embed")
        return None
    if _file_hash(source_path) != attachment_hash:
        stats.attachments_missing += 1
        logging.warning("Signal attachment content hash does not match; omitting its embed")
        return None

    original_name = _safe_filename(_string(pointer.get("fileName")))
    if original_name:
        extension = Path(original_name).suffix or source_path.suffix
        desired_name = Path(original_name).stem + extension
    else:
        local_time = datetime.fromtimestamp(timestamp)
        extension = source_path.suffix
        desired_name = f"signal-{local_time:%Y-%m-%d-%H%M%S}{extension}"
        if same_message_index > 1:
            stem = Path(desired_name).stem
            desired_name = f"{stem}-{same_message_index}{extension}"

    content_type = _string(
        pointer.get("contentType") or raw_attachment.get("contentType")
    )
    if not content_type.startswith("image/"):
        stats.skipped["unsupported non-image attachment"] += 1
        return None

    attachment = message_md.attachment.Attachment()
    attachment.id = attachment_hash
    attachment.type = content_type
    attachment.width = pointer.get("width") or raw_attachment.get("width") or 0
    attachment.height = pointer.get("height") or raw_attachment.get("height") or 0
    attachment.custom_filename = ""
    attachment.signal_desired_filename = desired_name
    attachment.signal_source_path = source_path
    return attachment


def load_messages(
    _dest_file,
    messages,
    _reactions,
    config_obj,
    stats,
    export_folder,
    prompt_for_groups=False,
    verbose=False,
):
    export_file = Path(config_obj.filename)
    recipients = {}
    chats = {}
    skipped_contact_warnings = set()
    skipped_group_warnings = set()
    prompted_groups = set()
    skipped_author_warnings = set()
    content_hash_index = {}

    def resolve_author(author_id, add_missing=False, warn_contact=False):
        if self_id and author_id == self_id:
            return config_obj.me
        recipient = recipients.get(_string(author_id))
        if recipient and recipient.kind == "contact":
            if add_missing:
                return _lookup_person(
                    recipient,
                    config_obj,
                    stats,
                    skipped_contact_warnings,
                    verbose=verbose,
                )
            if warn_contact:
                return _lookup_person(
                    recipient, config_obj, stats, skipped_contact_warnings,
                    update_aci=False, verbose=verbose,
                )
            return _find_configured_person(recipient, config_obj)
        return False

    self_id = ""
    group_slugs = {}
    configured_me = next(
        (person for person in config_obj.people if person.slug == config_obj.me.slug),
        None,
    )
    if configured_me:
        _ensure_person_first_name(configured_me, verbose=verbose)
        if not config_obj.me.first_name:
            config_obj.me.first_name = configured_me.first_name
    _ensure_person_first_name(config_obj.me, verbose=verbose)

    try:
        begin_date = date.fromisoformat(config_obj.from_date)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Invalid begin date: {config_obj.from_date!r}") from error

    for item in _iter_export_records(export_file, recipients, chats, config_obj, stats):
        if not self_id:
            self_id = next(
                (recipient_id for recipient_id, recipient in recipients.items()
                 if recipient.kind == "self"),
                "",
            )

        is_incoming = "incoming" in item
        is_outgoing = "outgoing" in item
        if is_incoming == is_outgoing:
            kind = next(
                (key for key in item if key not in {"chatId", "authorId", "dateSent",
                                                     "dateReceived", "standardMessage",
                                                     "revisions", "read", "linkPreview"}),
                "updateMessage",
            )
            stats.skipped[kind] += 1
            continue

        chat_id = _string(item.get("chatId"))
        recipient_id = chats.get(chat_id)
        chat_recipient = recipients.get(recipient_id)
        if not chat_recipient:
            stats.skipped["chat recipient unavailable"] += 1
            continue

        standard_message = item.get("standardMessage")
        if not isinstance(standard_message, dict):
            kind = next(
                (key for key in item if key not in {"chatId", "authorId", "dateSent",
                                                     "dateReceived", "incoming", "outgoing",
                                                     "revisions", "read", "linkPreview"}),
                "unsupported chatItem",
            )
            stats.skipped[kind] += 1
            continue

        date_sent = item.get("dateSent")
        try:
            timestamp_ms = int(date_sent)
            timestamp = timestamp_ms / 1000
            message_date = datetime.fromtimestamp(timestamp).date()
        except (TypeError, ValueError, OverflowError, OSError):
            stats.skipped["invalid timestamp"] += 1
            continue

        if message_date < begin_date:
            stats.skipped["before begin date"] += 1
            continue
        if config_obj.max_messages and len(messages) >= int(config_obj.max_messages):
            stats.skipped["maximum message count reached"] += 1
            continue

        if is_outgoing and chat_recipient.kind == "group":
            stats.skipped["outgoing group message"] += 1
            continue
        if chat_recipient.kind == "group":
            if chat_recipient.id not in group_slugs:
                group_slugs[chat_recipient.id] = config_obj.get_group_slug_by_signal_group_key(
                    chat_recipient.signal_group_key
                )
            group_slug = group_slugs[chat_recipient.id]
            if not group_slug:
                group_name = chat_recipient.group_title or "(untitled group)"
                fingerprint = chat_recipient.signal_group_key
                existing_config_group = next(
                    (
                        group
                        for group in config_obj.groups
                        if group.description == group_name
                    ),
                    None,
                )
                if (
                    prompt_for_groups
                    and fingerprint
                    and chat_recipient.id not in prompted_groups
                ):
                    prompted_groups.add(chat_recipient.id)
                    if existing_config_group:
                        prompt = (
                            f"Add fingerprint to existing groups.json entry "
                            f"{existing_config_group.slug!r} ({group_name!r})? "
                            f"{fingerprint} [y/N] "
                        )
                    else:
                        suggested_slug = re.sub(
                            r"[^a-z0-9]+", "-", group_name.lower()
                        ).strip("-")
                        prompt = (
                            f"Add Signal group {group_name!r} to groups.json "
                            f"as {suggested_slug!r} with fingerprint {fingerprint}? [y/N] "
                        )
                    answer = input(prompt).strip().lower()
                    if answer in {"y", "yes"}:
                        group_slug = _add_group_to_config(
                            chat_recipient, recipients, config_obj
                        )
                        group_slugs[chat_recipient.id] = group_slug
                if not group_slug:
                    if chat_recipient.id not in skipped_group_warnings:
                        if fingerprint:
                            if existing_config_group:
                                logging.warning(
                                    "Skipping messages for Signal group %r; add "
                                    '"signal-group-key": %s to existing groups.json entry %r',
                                    group_name,
                                    json.dumps(fingerprint),
                                    existing_config_group.slug,
                                )
                            else:
                                suggestion = _new_group_config_entry(
                                    chat_recipient,
                                    recipients,
                                    config_obj,
                                    {group.slug for group in config_obj.groups},
                                )
                                logging.warning(
                                    "Skipping messages for Signal group %r without a configured fingerprint; "
                                    "add this entry to groups.json: %s",
                                    group_name,
                                    json.dumps(suggestion, ensure_ascii=False),
                                )
                        else:
                            logging.warning(
                                "Skipping messages for Signal group %r because its fingerprint "
                                "could not be computed",
                                group_name,
                            )
                        skipped_group_warnings.add(chat_recipient.id)
                    stats.skipped["unconfigured group"] += 1
                    continue
            person_slug = ""
        elif chat_recipient.kind == "contact":
            person = _lookup_person(
                chat_recipient,
                config_obj,
                stats,
                skipped_contact_warnings,
                verbose=verbose,
            )
            if not person:
                stats.skipped["unconfigured contact"] += 1
                continue
            person_slug = person.slug
            group_slug = ""
        elif chat_recipient.kind == "self":
            person_slug = config_obj.me.slug
            group_slug = ""
        else:
            stats.skipped["unsupported chat recipient"] += 1
            continue

        if is_outgoing:
            from_slug = config_obj.me.slug
            to_slugs = [person_slug] if person_slug else []
        else:
            author_id = _string(item.get("authorId"))
            author = resolve_author(author_id, add_missing=True, warn_contact=True)
            if not author:
                if (not recipients.get(author_id)
                        and author_id not in skipped_author_warnings):
                    logging.warning("Skipping a message with an unconfigured sender")
                    skipped_author_warnings.add(author_id)
                stats.skipped["unconfigured message author"] += 1
                continue
            from_slug = author.slug
            to_slugs = [config_obj.me.slug] if not group_slug else []

        text_record = standard_message.get("text")
        body = text_record.get("body", "") if isinstance(text_record, dict) else ""
        if not isinstance(body, str):
            body = ""

        the_message = message.Message()
        the_message.timestamp = timestamp
        the_message.set_date_time()
        the_message.id = ""
        the_message.body = body
        the_message.from_slug = from_slug
        the_message.to_slugs = to_slugs
        the_message.group_slug = group_slug
        the_message.service = markdown.YAML_SERVICE_SIGNAL

        quote_record = standard_message.get("quote")
        if isinstance(quote_record, dict):
            the_message.quote.id = _string(quote_record.get("targetSentTimestamp"))
            quoted_text = quote_record.get("text")
            if isinstance(quoted_text, dict):
                the_message.quote.text = _string(quoted_text.get("body"))
            quote_author = resolve_author(
                _string(quote_record.get("authorId"))
            )
            if quote_author:
                the_message.quote.author_slug = quote_author.slug
                the_message.quote.author_name = getattr(
                    quote_author, "first_name", quote_author.identity.first_name
                )

        reaction_records = standard_message.get("reactions", [])
        if isinstance(reaction_records, list):
            for reaction_record in reaction_records:
                if not isinstance(reaction_record, dict):
                    stats.skipped["invalid reaction"] += 1
                    continue
                reaction_author_id = _string(reaction_record.get("authorId"))
                reaction_author = resolve_author(reaction_author_id)
                if not reaction_author:
                    if reaction_author_id not in skipped_author_warnings:
                        logging.warning("Dropping a reaction from an unconfigured contact")
                        skipped_author_warnings.add(reaction_author_id)
                    stats.skipped["unconfigured reaction author"] += 1
                    continue
                reaction = message.Reaction()
                reaction.emoji = _string(reaction_record.get("emoji"))
                reaction.from_slug = reaction_author.slug
                try:
                    reaction.target_time_sent = int(
                        reaction_record.get("sentTimestamp", 0)
                    ) / 1000
                except (TypeError, ValueError):
                    reaction.target_time_sent = 0
                the_message.reactions.append(reaction)

        attachments = standard_message.get("attachments", [])
        if isinstance(attachments, list):
            fallback_index = 0
            for attachment_record in attachments:
                pointer = (
                    attachment_record.get("pointer")
                    if isinstance(attachment_record, dict)
                    else None
                )
                filename = (
                    _safe_filename(_string(pointer.get("fileName")))
                    if isinstance(pointer, dict)
                    else ""
                )
                if not filename:
                    fallback_index += 1
                the_attachment = _parse_attachment(
                    attachment_record,
                    timestamp,
                    export_folder,
                    stats,
                    fallback_index or 1,
                    content_hash_index,
                )
                if the_attachment:
                    the_message.add_attachment(the_attachment)

        if not the_message.body and not the_message.attachments:
            stats.skipped["empty message"] += 1
            continue
        messages.append(the_message)

    return True


def _file_hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _choose_attachment_name(directory, desired_name, attachment_hash):
    candidate = desired_name
    existing_path = directory / candidate
    if existing_path.exists() and _file_hash(existing_path) != attachment_hash:
        name = Path(candidate)
        candidate = f"{name.stem}-{attachment_hash[:6]}{name.suffix}"
        suffix = 2
        while (directory / candidate).exists() and _file_hash(directory / candidate) != attachment_hash:
            candidate = f"{name.stem}-{attachment_hash[:6]}-{suffix}{name.suffix}"
            suffix += 1
    return candidate


def _copy_signal_attachments(entities, folder, config_obj, stats):
    names_by_hash = {}
    for entity in entities:
        for dated_messages in entity.messages:
            for the_message in dated_messages.messages:
                media_folder = Path(
                    message_md.markdown.get_media_folder_name(
                        entity, folder, the_message, config_obj
                    )
                ) / config_obj.media_subfolder
                for the_attachment in the_message.attachments:
                    attachment_hash = the_attachment.id
                    cache_key = (str(media_folder), attachment_hash)
                    if cache_key in names_by_hash:
                        the_attachment.custom_filename = names_by_hash[cache_key]
                        continue

                    media_folder.mkdir(parents=True, exist_ok=True)
                    desired_name = the_attachment.signal_desired_filename
                    final_name = _choose_attachment_name(
                        media_folder, desired_name, attachment_hash
                    )
                    destination = media_folder / final_name
                    if not destination.exists():
                        shutil.copy2(the_attachment.signal_source_path, destination)
                        stats.attachments_copied += 1
                    names_by_hash[cache_key] = final_name
                    the_attachment.custom_filename = final_name


def _generate_attachment_link(the_attachment, config_obj):
    filename = getattr(the_attachment, "custom_filename", "")
    if not filename:
        return the_attachment.generate_link_original(config_obj)

    link = ""
    if the_attachment.is_image() and config_obj.image_embed:
        link = "!"
    link += "[[" + filename
    image_width = (
        250 if filename.startswith(("Screenshot", "Spelling Bee"))
        else config_obj.image_width
    )
    if the_attachment.is_image() and image_width:
        link += "|" + str(image_width)
    return link + "]]\n"


def _install_shared_overrides(stats):
    original_setup_folders = message_md.setup_folders
    original_move_attachments = message_md.attachment.move_attachments
    attachment_class = message_md.attachment.Attachment
    original_generate_link = attachment_class.generate_link

    def preserve_export(config_obj):
        return config_obj.filename

    def copy_attachments(entities, folder, config_obj):
        _copy_signal_attachments(
            entities, folder, config_obj, stats
        )

    def generate_link(the_attachment, config_obj):
        if not hasattr(the_attachment, "generate_link_original"):
            the_attachment.generate_link_original = original_generate_link.__get__(
                the_attachment, attachment_class
            )
        return _generate_attachment_link(the_attachment, config_obj)

    message_md.setup_folders = preserve_export
    message_md.attachment.move_attachments = copy_attachments
    attachment_class.generate_link = generate_link
    return original_setup_folders, original_move_attachments, original_generate_link


def _restore_shared_overrides(overrides):
    message_md.setup_folders, message_md.attachment.move_attachments, generate_link = overrides
    message_md.attachment.Attachment.generate_link = generate_link


def _has_begin_option(argv):
    return any(arg == "-b" or arg == "--begin" or arg.startswith("--begin=") for arg in argv)


def _has_file_option(argv):
    return any(
        arg in ("-f", "--file")
        or arg.startswith("--file=")
        or (arg.startswith("-f") and arg != "-f")
        for arg in argv
    )


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    verbose = any(option in argv for option in ("-v", "--verbose"))
    argv = [arg for arg in argv if arg not in ("-v", "--verbose")]
    list_mode = None
    for option, mode in (("--list-groups", "groups"), ("--list-people", "people")):
        if option in argv:
            if list_mode:
                raise ValueError("Use only one list helper at a time")
            list_mode = mode
            argv.remove(option)

    if not _has_file_option(argv):
        argv.extend(["-f", "main.jsonl"])

    original_argv = sys.argv
    sys.argv = [sys.argv[0], *argv]
    stats = RunStats()
    the_config = config.Config()
    try:
        if not message_md.setup(the_config, markdown.YAML_SERVICE_SIGNAL):
            return 1

        if not _has_begin_option(argv):
            the_config.from_date = date.today().isoformat()

        export_folder = Path(the_config.source_folder).resolve()
        export_file = Path(the_config.filename).resolve()
        if export_file.parent != export_folder:
            raise ValueError("The JSONL input file must be directly inside the export folder")
        if export_file.name != "main.jsonl":
            logging.warning("Expected input filename main.jsonl; using %s", export_file.name)

        if list_mode:
            recipients = {}
            chats = {}
            for _ in _iter_export_records(
                export_file, recipients, chats, the_config, stats, list_mode
            ):
                pass
            return 0

        overrides = _install_shared_overrides(stats)

        def load_callback(dest_file, messages, reactions, config_obj):
            return load_messages(
                dest_file,
                messages,
                reactions,
                config_obj,
                stats,
                export_folder,
                prompt_for_groups=sys.stdin.isatty(),
                verbose=verbose,
            )

        try:
            message_md.get_markdown(
                the_config, load_callback, [], []
            )
        finally:
            _restore_shared_overrides(overrides)

        written = sum(
            len(day.messages)
            for entity in [*the_config.people, *the_config.groups]
            for day in entity.messages
        )
        skipped = ", ".join(
            f"{reason}: {count}"
            for reason, count in sorted(stats.skipped.items())
            if count
        ) or "none"
        print(f"Chat items read: {stats.chat_items_read}")
        print(f"Messages written: {written}")
        print(f"Skipped items: {skipped}")
        print(f"Attachments copied: {stats.attachments_copied}")
        print(f"Attachments missing: {stats.attachments_missing}")
        return 0
    finally:
        sys.argv = original_argv


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as error:
        logging.error("%s", error)
        raise SystemExit(1)
