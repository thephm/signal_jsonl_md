# Signal JSONL to Markdown Converter: Spec

Oct 9, 2026 · @Bob

## Overview

A new script, `signal_jsonl_md.py`, converts Signal Android's native export (`main.jsonl` plus `files/`) into the same per-day Markdown files that `signal_sqlite_md` produces today. It reuses code from `message_md`, `hal` (the `Person` class) and `hal_md`, and nothing from the `signal_sqlite_md` repo, which this tool is meant to replace. Only the parsing layer is new.

**Goals**

- Produce the same output as `signal_sqlite_md`: one `YYYY-MM-DD.md` per day per person or group, same frontmatter, same body formatting.
- Replace the CSV export and the manual attachment-download steps with one input folder.
- Resolve contacts straight from the export; groups need one new `groups.json` field.

**Non-goals**

- Edit history: only the latest text of an edited message is output.
- Account settings, keys, stickers, call links, distribution lists.
- Decryption: the export is already decrypted.
- Unsupported data: attachments other than images, videos, or audio, media that was not downloaded, mentions and text styling, view-once and deleted messages, note to self, outgoing group messages. Audio and video support was added after the original image-only sample; voice notes with an audio pointer are treated as ordinary audio attachments.

The schema below was inferred from one export (Signal Android, 2026-10-09). It is not taken from Signal documentation.

## Input

The input is one export folder: `main.jsonl`, `metadata.json` (just `{}` in the sample), `files/<xx>/<sha256-hex>.<ext>`, and `.nomedia` (an Android marker, ignored).

`main.jsonl` holds one JSON object per line, each with a single top-level key (the header line has none). Records appear in this fixed order:

| Order | Record | Count in sample | Use |
| --- | --- | --- | --- |
| 1 | Header: `version`, `backupTimeMs`, `mediaRootBackupKey` | 1 | Check `version` is `"1"`; never output the key |
| 2 | `account` | 1 | Not used |
| 3 | `recipient` | 3,791 | Contact, group or self, looked up by `id` |
| 4 | `chat` | 42 | Maps a chat `id` to its `recipientId` |
| 5 | `stickerPack` | 5 | Not used |
| 6 | `chatItem` | 206 | The messages |

Conventions seen in the sample:

- 64-bit values (ids, timestamps) are JSON strings. Timestamps are Unix milliseconds.
- `chatItem` records are not sorted by time, even within a chat.
- A `chatItem` has either `incoming` or `outgoing` (184 of 206) or neither, in which case it is an `updateMessage` (22 of 206).
- There is no message `id`. The pair (`chatId`, `dateSent`) was unique across the sample.

## Configuration

`settings.json`, `people.json`, `groups.json` and the `message_md` command-line options are reused unchanged, including `-b` (default: today). Three things are new.

**People.** A contact is matched to a person in this order: `recipient.contact.e164` to `mobile`, then `recipient.contact.aci` to a new `signal_aci` field. The ACI fallback covers Signal accounts that have no phone number. `signal_aci` lives in `people.json` only; nothing is added to `Person.md` files. `conversation_id` and `service_id` are not read. (A Signal service ID can be an ACI or a PNI, so `signal_aci` is the exact name.)

**Groups.** `groups.json` gains one field, `signal-group-key`, that identifies a group in the export. It is hyphenated like the existing `conversation-id` in that file. Its value is the SHA-256 hex digest of the decoded 32-byte `recipient.group.masterKey`. The master key itself is a secret and `groups.json` may be synced or committed, so only the fingerprint goes in config. Recipient ids are not assumed stable between exports, so they are not used. The group title is shown only as a setup aid.

**Setup helpers.** `--list-groups` prints each group chat's title, member count and fingerprint, so fingerprints can be pasted into `groups.json` instead of searched for by hand. `--list-people` prints each contact in the export with no match in `people.json`: phone number, ACI and the name in the export, so the ACI can be pasted into `people.json`. Both print to the console only and write no Markdown.

## Changes to shared code

The tool needs small additions to `hal` and `message_md`, each following a pattern already in `config.py`.

**`hal`**

- `socials.py`: add `signal_aci` to `Socials`, including its `__str__`.

**`message_md`**

- `config.py`, people: add `PERSON_FIELD_SIGNAL_ACI = "signal_aci"`. Read it in `load_people()` into `Person.socials.signal_aci` with `json_person.get`, since there is no legacy hyphenated key. Add `get_person_by_signal_aci()`, modelled on `get_person_by_linkedin_id()`. Write it in `save_people()`.
- `config.py`, groups: add `GROUP_FIELD_SIGNAL_GROUP_KEY = "signal-group-key"`. Read it in `load_groups()` as an optional field, as `conversation-id` is. Add a lookup modelled on `get_group_slug_by_conversation_id()`.
- `group.py`: add a `signal_group_key` attribute to `Group`, as `conversation_id` is.

**Why `save_people()` needs the change.** `get_or_create_person()` calls `save_people()`, which rewrites `people.json` from a fixed list of fields. Without the change, any other tool that creates a person would erase every `signal_aci`.

**Phone matching.** Use the existing `get_person_by_number()` on `recipient.contact.e164`. It compares the last 10 digits, so a number with or without `+1` matches. It is also loose across countries: two numbers with the same last 10 digits would collide. Skip phone matching when a contact has no `e164`.

**`service_id`.** `config.py` never loads it from `people.json`, so a `service_id` there has no effect. The new tool ignores it.

## Processing pipeline

The script supplies a `load_messages()` callback to `message_md`'s `get_markdown`. It builds `message_md` `Message` objects directly and imports nothing from `signal_sqlite_md`. Steps:

1. Stream `main.jsonl` line by line. Skip blank lines. On a line that fails to parse, log its line number and continue.
2. Read the header and stop unless `version` is `"1"`.
3. Build lookups: `recipients[id]`, `chats[id]`, and `self_id` (the recipient that has a `self` key). Ignore `account` and `stickerPack`.
4. For each `chatItem`, skip it if it has neither `incoming` nor `outgoing`. Skip it if its chat maps to a contact or group that is not in the config.
5. Build a `Message` from the mapping in the next section.
6. Drop messages with no body and no attachments, as today.
7. Sort all messages by `dateSent`, ascending.
8. Hand the list to `message_md` for grouping by person or group and by day, and for writing files.
9. Print counts at the end: messages written, items skipped by reason, attachments copied, attachments missing.

## Field mapping

How each `message_md` `Message` field is filled from a `chatItem`. In the sample, outgoing `authorId` always equals `self_id`. In one-to-one chats an incoming `authorId` equals the chat's `recipientId`. In group chats it is the sending member's recipient id.

| `Message` field | Source in export | Notes |
| --- | --- | --- |
| `id` | None | Left blank. (`chatId`, `dateSent`) is the unique key. |
| `timestamp`, `time`, `date_str`, `time_str` | `dateSent` | Milliseconds to seconds, then local time, as today. |
| `body` | `standardMessage.text.body` | Strip URL query strings, as today. |
| `from_slug` | Outgoing: me. Incoming: `authorId` to contact to person | Matching rules in Configuration. |
| `to_slugs` | Outgoing one-to-one: the chat's contact. Incoming: me | Empty for group messages. |
| `group_slug` | Chat recipient is a group, matched by `signal-group-key` |  |
| `service` | `signal` |  |
| `quote.id` | `standardMessage.quote.targetSentTimestamp` |  |
| `quote.text` | `standardMessage.quote.text.body` | The quote carries its own text, so no lookup of the target is needed. |
| `quote.author_slug` | `standardMessage.quote.authorId` | Resolved like `from_slug`. |
| `reactions` | `standardMessage.reactions[]`: `emoji`, `authorId`, `sentTimestamp` | Already nested in the message, so no timestamp matching through `add_reactions`. |
| `attachments` | `standardMessage.attachments[].pointer` | See Attachments. |
| Not mapped | `revisions[]`, `updateMessage`, `sendStatus`, `read`, `linkPreview` |  |

Edited messages: the record itself holds the latest text and `revisions[]` holds older versions. In the one edited message I inspected, every revision's `dateSent` was earlier than the record's. Only the record's text is used.

## Output format

The output format is owned by `message_md` and does not change. This tool must not add its own formatting. For reference, from reading `markdown.py`:

- **Files:** one `YYYY-MM-DD.md` per day, under `<output>/<people subfolder>/<slug>/` for a person or `<groups folder>/<slug>/` for a group. Notes to self go to the daily notes folder.
- **Frontmatter:** `tags` (`chat`, plus the group slug), `people`, `date`, `time`, `service: signal`, matching the `Chat.md` template in `hal_md`.
- **Message header:** the sender's first name and time (for example "Name at 10:20"), controlled by `settings.json`.
- **Body:** every line prefixed with ` >  `.
- **Quote:** shown above the body as `> > Author: text`, truncated at a maximum length.
- **Reactions:** `emoji *name*` after the body, if `include-reactions` is on.
- **Attachments:** Obsidian embeds, described in the next section.
- **Re-runs:** inherited from `message_md`. Messages are appended only to a file created within the last 30 seconds, so a re-run does not duplicate them.

## Attachments

Attachment files are found by content hash. All 25 files in the sample matched a reference (24 attachments and 1 link-preview image), with no hash used twice.

1. Take `pointer.locatorInfo.plaintextHash` (base64 SHA-256) and convert it to lowercase hex.
2. First look for `files/<first two hex characters>/<hex>.<ext>` by globbing `<hex>.*` rather than building the extension from the MIME type. If no file is found there, hash the files under `files/` by content and use a file whose SHA-256 matches. Some exports have hash-named files whose names do not match the plaintext content hash.
3. Choose the vault filename. A bare hash is never used as a name.
   - If `pointer.fileName` is present, use it, replacing characters unsafe in filenames or Obsidian links (`\ / : * ? " < > | # ^ [ ]`) with `-`. In the sample, 14 of 24 attachments have one: 14 of the 15 incoming and none of the 9 outgoing.
   - Otherwise build `signal-<YYYY-MM-DD>-<HHMMSS>.<ext>` from the message's `dateSent` in local time, adding `-2`, `-3` for further attachments in the same message.
   - If the name is already taken by a file with a different hash, insert `-` and the first 6 hex characters of the hash before the extension. In the sample, 1 of the 14 original names repeats.
   - The extension is the original name's if it has one, otherwise the source file's.
4. Copy or move the file to `<slug>/media/` under that name. If the same hash is referenced twice, copy the file once and link it twice.
5. Write `![[<name>|<width>]]` for images using the existing image settings. Video and audio MIME types (`video/*` and `audio/*`, including those in the shared `mime_types.json`) use `![[<name>]]` without a width, independently of the image-embed setting. Preserve filename/source extensions rather than deriving them from MIME type.

`message_md`'s `move_attachments` reads a flat folder and appends a suffix from the MIME type, which does not fit this export. The new tool overrides it with its own lookup and naming and leaves `message_md` unchanged.

If a file is missing or `wasDownloaded` is false, log a warning, keep the message text, and omit the embed.

Link-preview images are not embedded. In the one preview in the sample, the page URL is already in the message body.

## Edge cases and error handling

| Case | Behaviour |
| --- | --- |
| Unsorted `chatItem` records | Sort by `dateSent` before output. |
| Contact not in `people.json` | Skip its messages and warn once per contact. With `-a`, add the person, as `message_md` does. |
| Group with no `signal-group-key` match | Skip its messages and warn once per group. |
| Contact or group names | Taken from config, not the export, so missing profile or nickname fields do not matter. The export's names are used only by `--list-groups`. |
| Reaction author not in config | Drop that reaction and warn. |
| Quote author not in config | Keep the quote text, omit the author name. |
| Message with an attachment and no text | Output it. |
| Edited message | Latest text only. |
| `updateMessage` (identity change, group join, call, profile change) | Skipped. Counted in the summary. |
| Unknown record keys or `chatItem` bodies (for example deleted, view-once, sticker) | Skip, count by kind, and list the counts at the end. |
| Header `version` is not `"1"` | Stop with an error. |
| Note to self (chat whose recipient is `self`) | Not in the sample. Use `message_md`'s existing handling. Out of scope. |
| Outgoing message in a group | Not in the sample. Out of scope. |
| Encoding and time zone | Read and write UTF-8. Use the machine's local time zone, as today. |

## Security and privacy

The export contains secrets alongside the messages: the media root key in the header; the account's profile key and PIN; group master keys; and contacts' identity keys. The sample export also sat in a synced OneDrive folder.

- Read only the fields listed in this spec. Never print or write the header, `account`, or any key field.
- Debug output must not dump raw records.
- Output Markdown contains slugs, names from config, text, and attachment links only. It contains no phone numbers, service IDs or keys from the export.
- `groups.json` holds a fingerprint of a group master key, never the key.
- Test fixtures must be synthetic. Do not commit a real export, or fixtures derived from one.
- Recommend keeping the export folder out of synced and committed locations, and deleting it after conversion.

## Acceptance tests and open items

**Acceptance checks on the 2026-10-09 sample**, with every contact and group in config:

- 206 `chatItem` records are read. 184 are messages (94 outgoing, 90 incoming) and 22 are skipped as updates.
- All 184 messages have a body or an attachment, so 184 are written.
- All 25 attachment files are referenced and copied, and none is named with a bare hash.
- 45 reactions and 2 quotes appear in the output.
- The one edited message shows only its latest text.
- Running with a person or group missing from config skips only their messages and reports them.
- No key, PIN, phone number or service ID from the export appears in the output or the log.

**Open items**

1. Verify the group master key is unchanged between two exports of the same account. Only one export was available, so the fingerprint's stability is assumed.
2. Read `message_md`'s `group.py`, which has not been checked, before adding `signal_group_key` to `Group`.
3. Read `message_md.py`, which has not been checked, to confirm how `move_attachments` and `Attachment.generate_link` are called, so the new tool's override of them takes effect.
