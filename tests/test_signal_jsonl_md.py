import base64
import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import signal_jsonl_md as converter


class SignalJsonlMdTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.export_folder = Path(self.temp_dir.name)
        self.export_file = self.export_folder / "main.jsonl"
        converter.config._Config._instance = None
        self.the_config = converter.config.Config()
        self.the_config.me.slug = "me"
        self.the_config.me.first_name = "Me"
        self.the_config.me.identity.first_name = "Me"
        self.the_config.people = [self.the_config.me]
        self.the_config.from_date = "1970-01-01"
        self.the_config.max_messages = 10000
        self.the_config.create_people = False

        self.contact = converter.config.person.Person()
        self.contact.slug = "pat"
        self.contact.first_name = "Pat"
        self.contact.identity.first_name = "Pat"
        self.contact.identity.full_name = "Pat Example"
        self.contact.contact.mobile = ""
        self.contact.socials.signal_aci = "aci-pat"
        self.the_config.people.append(self.contact)

    def write_export(self, chat_items, extra_records=None):
        records = [
            {
                "version": "1",
                "backupTimeMs": "1704067200000",
                "mediaRootBackupKey": "header-secret",
            },
            {"account": {"profileKey": "account-key-secret", "pin": "synthetic-pin"}},
            {"recipient": {"id": "1", "self": {}}},
            {
                "recipient": {
                    "id": "2",
                    "contact": {
                        "aci": "aci-pat",
                        "profileGivenName": "Pat",
                        "profileFamilyName": "Example",
                    },
                }
            },
            {"chat": {"id": "10", "recipientId": "2"}},
        ]
        records.extend(extra_records or [])
        records.extend({"chatItem": item} for item in chat_items)
        self.export_file.write_text(
            "\n".join(json.dumps(record) for record in records) + "\n",
            encoding="utf-8",
        )
        self.the_config.filename = str(self.export_file)

    def test_aci_lookup_and_message_mapping(self):
        incoming = {
            "chatId": "10",
            "authorId": "2",
            "dateSent": "1704067200000",
            "incoming": {},
            "standardMessage": {
                "text": {"body": "Hello"},
                "quote": {
                    "targetSentTimestamp": "1704067199000",
                    "text": {"body": "Earlier"},
                    "authorId": "2",
                },
                "reactions": [
                    {"emoji": "👍", "authorId": "2", "sentTimestamp": "1704067201000"}
                ],
            },
        }
        self.write_export([incoming])
        messages = []
        stats = converter.RunStats()

        converter.load_messages(
            "", messages, [], self.the_config, stats, self.export_folder
        )

        self.assertEqual(len(messages), 1)
        the_message = messages[0]
        self.assertEqual(the_message.from_slug, "pat")
        self.assertEqual(the_message.to_slugs, ["me"])
        self.assertEqual(the_message.body, "Hello")
        self.assertEqual(the_message.quote.text, "Earlier")
        self.assertEqual(the_message.quote.author_slug, "pat")
        self.assertEqual(the_message.reactions[0].from_slug, "pat")
        self.assertEqual(the_message.service, "signal")
        self.assertEqual(stats.chat_items_read, 1)

    def test_missing_first_name_uses_signal_export_name_for_rendering(self):
        self.contact.first_name = ""
        self.contact.identity.full_name = ""
        incoming = {
            "chatId": "10",
            "authorId": "2",
            "dateSent": "1704067200000",
            "incoming": {},
            "standardMessage": {"text": {"body": "Hello"}},
        }
        self.write_export([incoming])
        messages = []
        stats = converter.RunStats()

        with patch("signal_jsonl_md.logging.warning") as warning:
            converter.load_messages(
                "", messages, [], self.the_config, stats, self.export_folder,
                verbose=False,
            )
            warning.assert_not_called()

        self.assertEqual(self.contact.first_name, "Pat")
        rendered = converter.markdown.format_markdown(
            messages[0], self.the_config, self.contact
        )
        self.assertIn("Pat ", rendered)
        self.assertIn("> Hello", rendered)

        self.contact.first_name = ""
        messages.clear()
        stats = converter.RunStats()
        with patch("signal_jsonl_md.logging.warning") as warning:
            converter.load_messages(
                "", messages, [], self.the_config, stats, self.export_folder,
                verbose=True,
            )
            warning.assert_called_once()
            self.assertIn(
                "no configured first_name", warning.call_args.args[0]
            )

    def test_group_key_is_a_digest_not_the_master_key(self):
        master_key = bytes(range(32))
        encoded = base64.b64encode(master_key).decode("ascii")

        fingerprint = converter._group_key_from_master_key(encoded)

        self.assertEqual(fingerprint, hashlib.sha256(master_key).hexdigest())
        self.assertNotEqual(fingerprint, encoded)
        self.assertEqual(converter._group_key_from_master_key("not-base64"), "")

    def test_group_chat_uses_fingerprint_lookup(self):
        master_key = bytes(range(32))
        encoded_key = base64.b64encode(master_key).decode("ascii")
        fingerprint = hashlib.sha256(master_key).hexdigest()
        group = converter.config.group.Group()
        group.slug = "friends"
        group.signal_group_key = fingerprint
        group.members = ["pat"]
        self.the_config.groups = [group]
        self.write_export(
            [{
                "chatId": "20",
                "authorId": "2",
                "dateSent": "1704067200000",
                "incoming": {},
                "standardMessage": {"text": {"body": "Group hello"}},
            }],
            extra_records=[
                {
                    "recipient": {
                        "id": "3",
                        "group": {
                            "title": "Friends",
                            "members": ["2"],
                            "masterKey": encoded_key,
                        },
                    }
                },
                {"chat": {"id": "20", "recipientId": "3"}},
            ],
        )
        messages = []
        stats = converter.RunStats()

        converter.load_messages(
            "", messages, [], self.the_config, stats, self.export_folder
        )

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].group_slug, "friends")
        self.assertEqual(messages[0].from_slug, "pat")

    def test_missing_group_fingerprint_warning_includes_group_name(self):
        master_key = bytes(range(32))
        self.write_export(
            [{
                "chatId": "20",
                "authorId": "2",
                "dateSent": "1704067200000",
                "incoming": {},
                "standardMessage": {"text": {"body": "Group hello"}},
            }],
            extra_records=[
                {
                    "recipient": {
                        "id": "3",
                        "group": {
                            "snapshot": {
                                "title": {"title": "Bottom of the 9"},
                                "members": [],
                            },
                            "members": ["2"],
                            "masterKey": base64.b64encode(master_key).decode("ascii"),
                        },
                    }
                },
                {"chat": {"id": "20", "recipientId": "3"}},
            ],
        )
        messages = []
        stats = converter.RunStats()

        with self.assertLogs(level="WARNING") as captured:
            converter.load_messages(
                "", messages, [], self.the_config, stats, self.export_folder
            )

        self.assertIn("Bottom of the 9", captured.output[0])
        self.assertIn("without a configured fingerprint", captured.output[0])
        self.assertIn(
            hashlib.sha256(master_key).hexdigest(), captured.output[0]
        )
        self.assertIn('"signal-group-key"', captured.output[0])
        self.assertIn('"slug": "bottom-of-the-9"', captured.output[0])
        self.assertEqual(messages, [])

    def test_prompt_can_add_missing_group_to_groups_json(self):
        master_key = bytes(range(32))
        fingerprint = hashlib.sha256(master_key).hexdigest()
        groups_file = self.export_folder / "groups.json"
        groups_file.write_text('{"groups": []}\n', encoding="utf-8")
        self.the_config.config_folder = str(self.export_folder)
        chat_item = {
            "chatId": "20",
            "authorId": "2",
            "dateSent": "1704067200000",
            "incoming": {},
            "standardMessage": {"text": {"body": "Group hello"}},
        }
        group_record = {
            "recipient": {
                "id": "3",
                "group": {
                    "snapshot": {
                        "title": {"title": "Bottom of the 9"},
                        "members": [{"userId": "2", "role": "ADMINISTRATOR"}],
                    },
                    "masterKey": base64.b64encode(master_key).decode("ascii"),
                },
            }
        }
        self.write_export(
            [chat_item, dict(chat_item)],
            extra_records=[
                group_record,
                {"chat": {"id": "20", "recipientId": "3"}},
            ],
        )
        messages = []
        stats = converter.RunStats()

        with patch("builtins.input", return_value="y") as prompt:
            converter.load_messages(
                "",
                messages,
                [],
                self.the_config,
                stats,
                self.export_folder,
                prompt_for_groups=True,
            )

        saved = json.loads(groups_file.read_text(encoding="utf-8"))
        self.assertEqual(len(saved["groups"]), 1)
        group = saved["groups"][0]
        self.assertEqual(group["description"], "Bottom of the 9")
        self.assertEqual(group["signal-group-key"], fingerprint)
        self.assertEqual(group["people"], ["pat"])
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0].group_slug, "bottom-of-the-9")
        prompt.assert_called_once()

    def test_prompt_adds_fingerprint_to_existing_matching_description(self):
        master_key = bytes(range(32))
        fingerprint = hashlib.sha256(master_key).hexdigest()
        groups_file = self.export_folder / "groups.json"
        existing_entry = {
            "id": "old-group-id",
            "slug": "bot9",
            "description": "Bottom of the 9",
            "people": ["pat", "ann"],
        }
        groups_file.write_text(
            json.dumps({"groups": [existing_entry]}, indent=2) + "\n",
            encoding="utf-8",
        )
        self.the_config.config_folder = str(self.export_folder)
        configured_group = converter.config.group.Group()
        configured_group.id = "old-group-id"
        configured_group.slug = "bot9"
        configured_group.description = "Bottom of the 9"
        configured_group.members = ["pat", "ann"]
        self.the_config.groups = [configured_group]

        chat_item = {
            "chatId": "20",
            "authorId": "2",
            "dateSent": "1704067200000",
            "incoming": {},
            "standardMessage": {"text": {"body": "Group hello"}},
        }
        self.write_export(
            [chat_item],
            extra_records=[
                {
                    "recipient": {
                        "id": "3",
                        "group": {
                            "snapshot": {
                                "title": {"title": "Bottom of the 9"},
                                "members": [{"userId": "2", "role": "ADMINISTRATOR"}],
                            },
                            "masterKey": base64.b64encode(master_key).decode("ascii"),
                        },
                    }
                },
                {"chat": {"id": "20", "recipientId": "3"}},
            ],
        )
        messages = []
        stats = converter.RunStats()

        with patch("builtins.input", return_value="y") as prompt:
            converter.load_messages(
                "",
                messages,
                [],
                self.the_config,
                stats,
                self.export_folder,
                prompt_for_groups=True,
            )

        saved = json.loads(groups_file.read_text(encoding="utf-8"))
        self.assertEqual(len(saved["groups"]), 1)
        self.assertEqual(saved["groups"][0]["id"], "old-group-id")
        self.assertEqual(saved["groups"][0]["slug"], "bot9")
        self.assertEqual(saved["groups"][0]["people"], ["pat", "ann"])
        self.assertEqual(saved["groups"][0]["signal-group-key"], fingerprint)
        self.assertEqual(configured_group.signal_group_key, fingerprint)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].group_slug, "bot9")
        self.assertIn("existing groups.json entry 'bot9'", prompt.call_args.args[0])
        prompt.assert_called_once()

    def test_list_helpers_print_only_config_setup_fields(self):
        master_key = bytes(range(32))
        encoded_key = base64.b64encode(master_key).decode("ascii")
        fingerprint = hashlib.sha256(master_key).hexdigest()
        self.write_export(
            [],
            extra_records=[
                {
                    "recipient": {
                        "id": "3",
                        "group": {
                            "title": "Friends",
                            "members": ["2"],
                            "masterKey": encoded_key,
                        },
                    }
                },
                {
                    "recipient": {
                        "id": "4",
                        "group": {
                            "title": "No chat",
                            "members": [],
                            "masterKey": encoded_key,
                        },
                    }
                },
                {
                    "recipient": {
                        "id": "5",
                        "contact": {
                            "e164": "+12025550199",
                            "aci": "aci-other",
                            "profileGivenName": "Other",
                            "profileFamilyName": "Contact",
                        },
                    }
                },
                {"chat": {"id": "20", "recipientId": "3"}},
            ],
        )
        recipients = {}
        chats = {}
        group_output = io.StringIO()
        with contextlib.redirect_stdout(group_output):
            list(converter._iter_export_records(
                self.export_file,
                recipients,
                chats,
                self.the_config,
                converter.RunStats(),
                "groups",
            ))
        self.assertEqual(
            group_output.getvalue().strip(), f"Friends\t1\t{fingerprint}"
        )

        recipients = {}
        people_output = io.StringIO()
        with contextlib.redirect_stdout(people_output):
            list(converter._iter_export_records(
                self.export_file,
                recipients,
                {},
                self.the_config,
                converter.RunStats(),
                "people",
            ))
        self.assertEqual(
            people_output.getvalue().strip(),
            "+12025550199\taci-other\tOther Contact",
        )

    def test_shared_config_loads_looks_up_and_saves_signal_identifiers(self):
        config_folder = self.export_folder / "config"
        config_folder.mkdir()
        (config_folder / "people.json").write_text(
            json.dumps([
                {"slug": "me", "first_name": "Me", "last_name": ""},
                {
                    "slug": "pat",
                    "first_name": "Pat",
                    "last_name": "Example",
                    "signal_aci": "aci-pat",
                },
            ]),
            encoding="utf-8",
        )
        (config_folder / "groups.json").write_text(
            json.dumps({
                "groups": [{
                    "id": "group-id",
                    "slug": "friends",
                    "description": "Friends",
                    "people": ["pat"],
                    "signal-group-key": "a" * 64,
                }]
            }),
            encoding="utf-8",
        )
        converter.config._Config._instance = None
        the_config = converter.config.Config()
        the_config.config_folder = str(config_folder)
        the_config.me.slug = "me"

        self.assertEqual(the_config.load_people(), 2)
        self.assertEqual(the_config.load_groups(), 1)
        self.assertEqual(the_config.get_person_by_signal_aci("aci-pat").slug, "pat")
        self.assertEqual(
            the_config.get_group_slug_by_signal_group_key("a" * 64), "friends"
        )
        self.assertTrue(the_config.save_people())
        saved_people = json.loads((config_folder / "people.json").read_text(encoding="utf-8"))
        self.assertEqual(saved_people[1]["signal_aci"], "aci-pat")
        self.assertEqual(the_config.groups[0].signal_group_key, "a" * 64)

    def test_image_link_width_depends_on_filename_prefix(self):
        self.the_config.image_width = 450
        self.the_config.image_embed = True
        for filename, width in (
            ("Screenshot 2026-10-08.jpg", 250),
            ("Spelling Bee Hints.jpg", 250),
            ("Spelling Bee Hints-44d6e1.jpg", 250),
            ("Photo.jpg", 450),
            ("My Screenshot.jpg", 450),
        ):
            with self.subTest(filename=filename):
                attachment = converter.message_md.attachment.Attachment()
                attachment.type = "image/jpeg"
                attachment.custom_filename = filename
                self.assertEqual(
                    converter._generate_attachment_link(attachment, self.the_config),
                    f"![[{filename}|{width}]]\n",
                )

    def test_attachment_is_found_by_hash_and_safely_named(self):
        source_bytes = b"synthetic image bytes"
        attachment_hash = hashlib.sha256(source_bytes).digest()
        hash_hex = attachment_hash.hex()
        source_folder = self.export_folder / "files" / hash_hex[:2]
        source_folder.mkdir(parents=True)
        (source_folder / f"{hash_hex}.jpg").write_bytes(source_bytes)
        attachment_record = {
            "pointer": {
                "locatorInfo": {
                    "plaintextHash": base64.b64encode(attachment_hash).decode("ascii")
                },
                "fileName": "Photo #1?.jpg",
                "contentType": "image/jpeg",
                "width": 640,
            }
        }
        stats = converter.RunStats()

        attachment = converter._parse_attachment(
            attachment_record, 1704067200, self.export_folder, stats, 1
        )

        self.assertIsNotNone(attachment)
        self.assertEqual(attachment.id, hash_hex)
        self.assertEqual(attachment.signal_desired_filename, "Photo -1-.jpg")
        self.assertEqual(attachment.width, 640)
        self.assertEqual(stats.attachments_missing, 0)

    def test_attachment_is_found_by_content_when_export_filename_differs(self):
        source_bytes = b"synthetic image bytes"
        attachment_hash = hashlib.sha256(source_bytes).digest()
        hash_hex = attachment_hash.hex()
        different_name = hashlib.sha256(b"different filename hash").hexdigest()
        source_folder = self.export_folder / "files" / different_name[:2]
        source_folder.mkdir(parents=True)
        source_path = source_folder / f"{different_name}.jpeg"
        source_path.write_bytes(source_bytes)
        attachment_record = {
            "pointer": {
                "locatorInfo": {
                    "plaintextHash": base64.b64encode(attachment_hash).decode("ascii")
                },
                "fileName": "Photo.jpg",
                "contentType": "image/jpeg",
            }
        }
        stats = converter.RunStats()

        attachment = converter._parse_attachment(
            attachment_record, 1704067200, self.export_folder, stats, 1
        )

        self.assertIsNotNone(attachment)
        self.assertEqual(attachment.id, hash_hex)
        self.assertEqual(attachment.signal_source_path, source_path)
        self.assertEqual(stats.attachments_missing, 0)

    def test_updates_and_empty_messages_are_counted_and_skipped(self):
        self.write_export([
            {
                "chatId": "10",
                "dateSent": "1704067200000",
                "updateMessage": {},
            },
            {
                "chatId": "10",
                "authorId": "2",
                "dateSent": "1704067201000",
                "incoming": {},
                "standardMessage": {"text": {"body": ""}, "attachments": []},
            },
        ])
        messages = []
        stats = converter.RunStats()

        converter.load_messages(
            "", messages, [], self.the_config, stats, self.export_folder
        )

        self.assertEqual(messages, [])
        self.assertEqual(stats.chat_items_read, 2)
        self.assertEqual(stats.skipped["updateMessage"], 1)
        self.assertEqual(stats.skipped["empty message"], 1)

    def test_end_to_end_writes_markdown_and_copies_media_without_moving_export(self):
        source_bytes = b"synthetic image bytes"
        attachment_hash = hashlib.sha256(source_bytes).digest()
        hash_hex = attachment_hash.hex()
        source_folder = self.export_folder / "files" / hash_hex[:2]
        source_folder.mkdir(parents=True)
        (source_folder / f"{hash_hex}.jpg").write_bytes(source_bytes)
        incoming = {
            "chatId": "10",
            "authorId": "2",
            "dateSent": "1704067200000",
            "incoming": {},
            "standardMessage": {
                "text": {"body": "Photo"},
                "attachments": [{
                    "pointer": {
                        "locatorInfo": {
                            "plaintextHash": base64.b64encode(attachment_hash).decode("ascii")
                        },
                        "fileName": "Picture #1.jpg",
                        "contentType": "image/jpeg",
                        "width": 640,
                    }
                }] * 2,
            },
        }
        self.write_export([incoming])
        self.the_config.output_folder = str(self.export_folder / "vault")
        self.the_config.people_subfolder = "People"
        self.the_config.groups_subfolder = "groups"
        self.the_config.people_folder = str(
            Path(self.the_config.output_folder) / self.the_config.people_subfolder
        )
        self.the_config.groups_folder = str(
            Path(self.the_config.people_folder) / self.the_config.groups_subfolder
        )
        media_folder = Path(self.the_config.people_folder) / "pat" / "media"
        media_folder.mkdir(parents=True)
        (media_folder / "Picture -1.jpg").write_bytes(b"different existing image")
        stats = converter.RunStats()
        overrides = converter._install_shared_overrides(stats)

        def load_callback(dest_file, messages, reactions, config_obj):
            return converter.load_messages(
                dest_file, messages, reactions, config_obj, stats, self.export_folder
            )

        try:
            converter.message_md.get_markdown(
                self.the_config, load_callback, [], []
            )
        finally:
            converter._restore_shared_overrides(overrides)

        self.assertEqual(
            sum(len(day.messages) for day in self.contact.messages),
            1,
            f"message grouping failed; date={getattr(self.contact.messages[0].messages[0], 'date_str', '') if self.contact.messages else 'none'}",
        )
        markdown_file = (
            Path(self.the_config.people_folder)
            / "pat"
            / f"{datetime.fromtimestamp(1704067200):%Y-%m-%d}.md"
        )
        chosen_name = f"Picture -1-{hash_hex[:6]}.jpg"
        media_file = (
            Path(self.the_config.people_folder) / "pat" / "media" / chosen_name
        )
        self.assertTrue(
            markdown_file.exists(),
            f"expected {markdown_file}; found {list(self.export_folder.rglob('*.md'))}",
        )
        output_text = markdown_file.read_text(encoding="utf-8")
        self.assertEqual(output_text.count(f"![[{chosen_name}|450]]"), 2)
        self.assertNotIn("header-secret", output_text)
        self.assertNotIn("account-key-secret", output_text)
        self.assertNotIn("synthetic-pin", output_text)
        self.assertEqual(media_file.read_bytes(), source_bytes)
        self.assertTrue(self.export_file.exists())
        self.assertEqual(stats.attachments_copied, 1)

    def test_consecutive_messages_from_same_sender_share_one_header(self):
        self.contact.identity.first_name = "Bernie"
        self.contact.first_name = "Bernie"
        ann = converter.config.person.Person()
        ann.slug = "ann"
        ann.first_name = "Ann"
        ann.identity.first_name = "Ann"
        self.the_config.people.append(ann)
        self.the_config.include_reactions = True
        self.the_config.service = "signal"
        self.the_config.output_folder = str(self.export_folder / "vault")
        self.the_config.people_subfolder = "People"
        self.the_config.people_folder = str(
            Path(self.the_config.output_folder) / self.the_config.people_subfolder
        )

        messages = []
        for body in ("Nice spot", "Doing okay, some pain, but the Advil kicked in."):
            the_message = converter.message.Message()
            the_message.timestamp = 1704067200
            the_message.set_date_time()
            the_message.from_slug = "pat"
            the_message.to_slugs = ["me"]
            the_message.body = body
            messages.append(the_message)

        reaction = converter.message.Reaction()
        reaction.emoji = "❤️"
        reaction.from_slug = "ann"
        messages[1].reactions.append(reaction)
        image_message = converter.message.Message()
        image_message.timestamp = 1704067201
        image_message.set_date_time()
        image_message.from_slug = "pat"
        image_message.to_slugs = ["me"]
        image = converter.message_md.attachment.Attachment()
        image.generate_link = lambda config_obj: "![[photo.jpg|450]]\n"
        image_message.add_attachment(image)
        messages.append(image_message)
        converter.message.add_messages(messages, self.the_config)

        converter.message_md.markdown.create_markdown_files(
            self.contact,
            Path(self.the_config.people_folder) / self.contact.slug,
            self.the_config,
        )

        output_file = (
            Path(self.the_config.people_folder)
            / self.contact.slug
            / f"{datetime.fromtimestamp(1704067200):%Y-%m-%d}.md"
        )
        output_text = output_file.read_text(encoding="utf-8")
        self.assertEqual(output_text.count("Bernie"), 1)
        self.assertIn(
            "> Nice spot\n> \n> Doing okay, some pain, but the Advil kicked in.",
            output_text,
        )
        self.assertIn("> ❤️ *ann*", output_text)
        self.assertIn("*ann*   \n\n![[photo.jpg|450]]", output_text)
        self.assertNotIn("> \n![[photo.jpg|450]]", output_text)

    def test_cli_setup_uses_shared_config_and_writes_output(self):
        self.write_export([{
            "chatId": "10",
            "authorId": "2",
            "dateSent": "1704067200000",
            "incoming": {},
            "standardMessage": {"text": {"body": "CLI test"}},
        }])
        config_folder = self.export_folder / "config"
        config_folder.mkdir()
        (config_folder / "people.json").write_text(
            json.dumps([
                {"slug": "me", "first_name": "Me", "last_name": ""},
                {
                    "slug": "pat",
                    "first_name": "Pat",
                    "last_name": "Example",
                    "signal_aci": "aci-pat",
                },
            ]),
            encoding="utf-8",
        )
        (config_folder / "groups.json").write_text(
            json.dumps({
                "groups": [{
                    "id": "configured-group",
                    "slug": "configured-group",
                    "description": "Synthetic group",
                    "people": ["pat"],
                }]
            }),
            encoding="utf-8",
        )
        output_folder = self.export_folder / "vault"
        converter.config._Config._instance = None

        result = converter.main([
            "-s", str(self.export_folder),
            "-c", str(config_folder),
            "-o", str(output_folder),
            "-m", "me",
            "-b", "1970-01-01",
            "-v",
        ])

        output_file = (
            output_folder
            / "People"
            / "pat"
            / f"{datetime.fromtimestamp(1704067200):%Y-%m-%d}.md"
        )
        self.assertEqual(result, 0)
        self.assertIn("CLI test", output_file.read_text(encoding="utf-8"))
        self.assertTrue(self.export_file.exists())


if __name__ == "__main__":
    unittest.main()
