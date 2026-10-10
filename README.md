# Signal JSONL to Markdown

`signal_jsonl_md.py` converts a Signal Android data export into per-day Markdown conversation files for an Obsidian vault. It reads Signal's `main.jsonl` and media files directly, so a separate CSV export and manual attachment download are not needed.

The converter handles the export parsing and builds `message_md` message objects. Shared code owns the Markdown format, conversation grouping, settings, and command-line behavior. It uses `hal`'s `Person` model and the `Chat.md` format from `hal_md`. It does not use code from `signal_sqlite_md`.

## Export data from Signal

On Android, open **Settings > Chats > Export Chat History > Export with media**, then choose a folder. Keep the export folder intact; the converter needs both `main.jsonl` and the accompanying `files/` directory.

The expected folder contains:

```text
export/
  main.jsonl
  metadata.json
  files/
    <first-two-hash-characters>/
      <sha256-hash>.<extension>
```

The converter supports export version `"1"`. The export schema described here is based on a Signal Android export dated 2026-10-09 and is not a general guarantee of Signal's export format.

## Run

The script uses the sibling `hal` and `message_md` repositories. Keep this repository alongside them, then run:

```powershell
python signal_jsonl_md.py -s "C:\path\to\export" -c "C:\path\to\config" -o "C:\path\to\vault" -m my-slug
```

`-s` is the Signal export folder. The script reads `main.jsonl` from that folder automatically; `-f` is not needed. As with `message_md`, `-c` points to the folder containing `settings.json`, `people.json`, and `groups.json`; `-o` is the vault output folder; and `-m` identifies your person slug. The default begin date is today. Add `-b 1970-01-01` to process the full export. Add `-v` (or `--verbose`) to show warnings about configured people whose `first_name` is missing. To configure identities before converting, add `--list-groups` or `--list-people` to the same command. These helpers require the export and config options and print setup data without generating Markdown.

Run the synthetic test suite with `python -m unittest discover -s tests`.

## Configuration

The converter reuses `settings.json`, `people.json`, `groups.json`, and the relevant `message_md` command-line options.

### People

Contacts are matched to configured people by phone number first, then by Signal ACI:

1. `recipient.contact.e164` matches the person's `mobile` value using `message_md`'s existing phone lookup (last 10 digits).
2. If there is no phone match, `recipient.contact.aci` matches the `signal_aci` field in `people.json`.

`signal_aci` is specific to `people.json`; it is not written to `Person.md`. If a configured person has no `first_name`, the converter uses the first word of their configured profile full name, then the Signal export contact name, for message headers. This is an in-memory fallback and does not update `people.json`; use `-v` to see a warning for each affected person and set `first_name` in the matching entry to avoid the fallback. With `-a`, unmatched contacts can be added as `message_md` does; otherwise their messages are skipped with a warning.

### Groups

Add `signal-group-key` to a group entry in `groups.json`. Its value is the lowercase hexadecimal SHA-256 digest of the decoded 32-byte `recipient.group.masterKey`. The key itself must not be copied into configuration. Recipient IDs are not used because they may change between exports.

Run with `--list-groups` to print group titles, member counts, and fingerprints for configuration. Run with `--list-people` to print contacts that do not match `people.json`, including their phone number, ACI, and export name. These helper options print to the console and do not generate Markdown.

When an unconfigured group is encountered in an interactive terminal, the converter asks whether to add it to `groups.json`. Confirming adds a group entry with a suggested slug, the fingerprint, and members matched to configured people. Declining leaves the file unchanged and prints a suggested JSON entry in the warning. In non-interactive runs, no prompt is shown; the suggested entry is logged.

## Conversion behavior

The converter:

1. Reads `main.jsonl` line by line, skips blank lines, and logs malformed lines by line number without printing their contents.
2. Checks that the export header version is `"1"`; it stops with an error for other versions.
3. Builds recipient and chat lookups, then skips unsupported update records and conversations without a configured person or group.
4. Converts supported incoming and outgoing chat items to `message_md` messages, using the latest text for edited messages.
5. Sorts messages by `dateSent` before handing them to `message_md`.
6. Lets `message_md` group messages by person or group and local calendar day, and write the Markdown files.

The message body, quote text and author, and reactions are read from the corresponding `standardMessage` fields. Reaction authors that cannot be matched are dropped with a warning; quote text is retained if its author cannot be matched.

`-b` is the shared `message_md` option for the starting date and defaults to today. Other input/output options are those provided by `message_md`; this converter does not define a separate Markdown format.

## Output

`message_md` writes one `YYYY-MM-DD.md` file per day in the configured output location: person conversations go under the configured people folder and person slug; group conversations go under the configured groups folder and group slug. It supplies the existing frontmatter (`tags`, `people`, `date`, `time`, and `service: signal`, with the group slug included in group tags) based on the `Chat.md` template in `hal_md`. It also supplies sender/time headers, quoted text, optional reactions, and attachment embeds. Message lines are prefixed with ` > `.

Consecutive messages from the same sender in the same conversation share one sender/time header. Text messages are separated by a quoted blank line; images start after an unquoted blank line. A message from another sender starts a new header.

Attachments are located in `files/` by the SHA-256 hash in the export, rather than by MIME type. The converter first checks the hash-derived path, then falls back to hashing files under `files/` by content; this handles exports whose media filenames differ from the plaintext content hashes. It copies matching media to the conversation's `media/` folder and embeds images using Obsidian links. It uses the supplied filename when available, sanitizes unsafe characters, and otherwise creates a timestamp-based name. If a filename conflicts with a different file, a short hash suffix is added. Repeated references to the same hash reuse one copied file.

Missing or undownloaded media is reported as a warning; the message text is kept and the embed is omitted. Link-preview images are not embedded. The end-of-run summary reports messages written, skipped items by reason, attachments copied, and missing attachments.

Image filenames starting with `Screenshot` or `Spelling Bee` use an embed width of 250. Other images use the configured `image-width` (normally 450). Prefix matching is case-sensitive.

As with `message_md`, a message is appended only to a matching file created within the last 30 seconds. This supports a quick re-run without duplicating messages; it is not a general-purpose deduplication mechanism for older files.

## Scope and limitations

- Only supported incoming and outgoing message records are converted. Update records (such as identity changes, group joins, calls, and profile changes) are skipped.
- Only the latest text of an edited message is output.
- Unsupported or absent export data—including non-image attachments, media not downloaded in the export, voice notes, mentions and text styling, view-once or deleted messages, note to self, and outgoing group messages—is outside the current scope.
- Account settings, keys, stickers, call links, and distribution lists are not processed.
- Timestamps are rendered in the machine's local time zone. Input and output use UTF-8.

## Privacy

Signal exports can contain account and profile keys, a PIN, group master keys, and contact identity keys. Keep the export folder out of synced or committed locations, and delete it when conversion is complete.

The converter reads only the fields needed for conversion. It must not print or write raw records or key fields. Generated Markdown contains message content, configured names and slugs, and attachment links; it does not include phone numbers, service IDs, or keys from the export. Group configuration stores only the master-key fingerprint, never the master key.
