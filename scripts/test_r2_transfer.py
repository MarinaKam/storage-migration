"""Offline migration checks using generated images and an in-memory S3 double."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import r2_transfer as r
from botocore.exceptions import ClientError
from PIL import Image


def error(code, status):
    return ClientError(
        {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}, "GetObject"
    )


class Store:
    def __init__(self):
        self.objects = {}
        self.puts = 0
        self.corrupt = False
        self.race = None

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise error("NoSuchKey", 404)
        data = self.objects[Key]
        return {"Body": io.BytesIO(data), "ContentLength": len(data)}

    def put_object(self, Bucket, Key, Body, IfNoneMatch, **kwargs):
        assert IfNoneMatch == "*"
        if self.race is not None:
            self.objects[Key] = self.race
        if Key in self.objects:
            raise error("PreconditionFailed", 412)
        self.puts += 1
        self.objects[Key] = b"corrupt" if self.corrupt else Body


class TransferTests(unittest.TestCase):
    def setUp(self):
        r.STOP.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.images = self.root / "images"
        self.images.mkdir()
        buf = io.BytesIO()
        Image.new("RGB", (3, 3)).save(buf, format="PNG")
        self.data = buf.getvalue()
        self.store = Store()
        self.state = self.root / "state"
        self.state.mkdir()
        self.plan = {"bucket": "sample-bucket", "id": "snapshot"}
        self.item = {
            "id": "sample",
            "key": "dataset/images/sample.png",
            "url": "https://images.example.com/sample.png",
            "source_ref": "source-reference",
            "local": self.images / "sample.jpg",
        }

    def test_local_copy_and_resume_consume_remote_bytes(self):
        self.item["local"].write_bytes(self.data)
        row = r.transfer_image(self.store, self.plan, self.item, self.state)
        self.assertEqual(row["status"], "copied_verified")
        self.assertEqual(self.store.objects[self.item["key"]], self.data)
        with patch.object(
            r, "source_bytes", side_effect=AssertionError("Unexpected source request")
        ):
            self.assertEqual(
                r.transfer_image(self.store, self.plan, self.item, self.state)["status"],
                "existing_verified",
            )
        self.assertEqual(self.store.puts, 1)

    def test_http_copy_has_no_local_photo_and_resume_uses_receipt(self):
        with patch.object(r, "source_bytes", return_value=self.data):
            self.assertEqual(
                r.transfer_image(self.store, self.plan, self.item, self.state)["status"],
                "copied_verified",
            )
        self.assertFalse(self.item["local"].exists())
        with patch.object(r, "source_bytes", side_effect=AssertionError("Unexpected refetch")):
            self.assertEqual(
                r.transfer_image(self.store, self.plan, self.item, self.state)["status"],
                "existing_verified",
            )

    def test_remote_mutation_and_local_mutation_are_rejected(self):
        self.item["local"].write_bytes(self.data)
        r.transfer_image(self.store, self.plan, self.item, self.state)
        for mutated in (b"wrong", self.data + b"changed", self.data[:-1]):
            self.store.objects[self.item["key"]] = mutated
            self.assertEqual(
                r.transfer_image(self.store, self.plan, self.item, self.state)["error_type"],
                "ConflictError",
            )
        self.store.objects[self.item["key"]] = self.data
        self.item["local"].write_bytes(self.data + b"changed")
        self.assertEqual(
            r.transfer_image(self.store, self.plan, self.item, self.state)["error_type"],
            "ConflictError",
        )
        self.assertEqual(self.store.puts, 1)

    def test_put_readback_corruption_never_creates_verified_receipt(self):
        self.item["local"].write_bytes(self.data)
        self.store.corrupt = True
        self.assertEqual(
            r.transfer_image(self.store, self.plan, self.item, self.state)["status"], "failed"
        )
        self.assertEqual(list(self.state.iterdir()), [])

    def test_conditional_race_never_overwrites(self):
        for raced in (self.data, b"other-writer"):
            store = Store()
            store.race = raced
            if raced == self.data:
                r.ensure_object(
                    store, "sample-bucket", "key", self.data, "image/png", r.IMAGE_LIMIT
                )
            else:
                with self.assertRaises(r.ConflictError):
                    r.ensure_object(
                        store, "sample-bucket", "key", self.data, "image/png", r.IMAGE_LIMIT
                    )
            self.assertEqual(store.objects["key"], raced)
            self.assertEqual(store.puts, 0)

    def test_invalid_local_image_symlink_and_stop_do_not_upload(self):
        self.item["local"].write_bytes(b"<html>error</html>")
        self.assertEqual(
            r.transfer_image(self.store, self.plan, self.item, self.state)["status"], "failed"
        )
        self.item["local"].unlink()
        elsewhere = self.root / "elsewhere"
        elsewhere.write_bytes(self.data)
        self.item["local"].symlink_to(elsewhere)
        self.assertEqual(
            r.transfer_image(self.store, self.plan, self.item, self.state)["status"], "failed"
        )
        r.STOP.set()
        self.assertEqual(
            r.transfer_image(self.store, self.plan, self.item, self.state)["status"], "stopped"
        )
        self.assertEqual(self.store.puts, 0)

    def test_lost_upload_ack_recovers_without_overwrite(self):
        self.item["local"].write_bytes(self.data)
        original = self.store.put_object

        def lost_ack(**kwargs):
            original(**kwargs)
            raise TimeoutError()

        self.store.put_object = lost_ack
        self.assertEqual(
            r.transfer_image(self.store, self.plan, self.item, self.state)["status"], "failed"
        )
        self.store.put_object = original
        self.assertEqual(
            r.transfer_image(self.store, self.plan, self.item, self.state)["status"],
            "existing_verified",
        )
        self.assertEqual(self.store.puts, 1)

    def test_access_denied_is_not_treated_as_missing(self):
        self.item["local"].write_bytes(self.data)
        with patch.object(self.store, "get_object", side_effect=error("AccessDenied", 403)):
            row = r.transfer_image(self.store, self.plan, self.item, self.state)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["http_status"], 403)
        self.assertEqual(self.store.puts, 0)

    def fixture(self):
        local = self.root / ".local"
        local.mkdir()
        manifest = self.root / "manifest.csv"
        manifest.write_text(
            "image_id,s3_url\nalpha,https://images.example.com/alpha.png\nbeta,https://images.example.com/beta.png\n"
        )
        tags = self.root / "tags.json"
        tags.write_text('{"tags": []}')
        (local / "dataset.json").write_text(
            json.dumps(
                {
                    "manifest": str(manifest),
                    "images": str(self.images),
                    "allowed_source_hosts": ["images.example.com"],
                }
            )
        )
        (local / "r2-transfer.json").write_text(
            json.dumps({"prefix": "dataset", "metadata_files": [str(manifest), str(tags)]})
        )
        (local / "credentials.json").write_text(
            json.dumps(
                {
                    "endpoint": "https://" + "a" * 32 + ".r2.cloudflarestorage.com",
                    "bucket": "sample-bucket",
                    "access_key_id": "dummy",
                    "secret_access_key": "dummy",
                }
            )
        )
        (self.images / "alpha.jpg").write_bytes(self.data)
        return manifest, tags

    def test_full_cli_plan_transfer_resume_and_metadata_conflict(self):
        manifest, tags = self.fixture()
        with (
            patch.object(r, "__file__", str(self.root / "scripts/r2_transfer.py")),
            patch.object(r.sys, "argv", ["r2_transfer.py", "--plan"]),
            patch.object(r, "client_for", side_effect=AssertionError("Plan used network")),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(r.main(), 0)
        with (
            patch.object(r, "__file__", str(self.root / "scripts/r2_transfer.py")),
            patch.object(r.sys, "argv", ["r2_transfer.py", "--all"]),
            patch.object(r, "client_for", return_value=self.store),
            patch.object(r, "source_bytes", return_value=self.data),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(r.main(), 0)
            self.assertEqual(r.main(), 0)
            self.assertEqual(
                self.store.objects["dataset/metadata/manifest.csv"], manifest.read_bytes()
            )
            self.assertEqual(self.store.objects["dataset/metadata/tags.json"], tags.read_bytes())
            self.assertEqual(self.store.puts, 4)
            self.store.objects["dataset/metadata/tags.json"] = b"changed"
            self.assertEqual(r.main(), 1)
        self.assertEqual(len(list((self.root / "logs").glob("*.jsonl"))), 2)

    def test_extensionless_url_has_stable_key_and_content_type(self):
        manifest, _tags = self.fixture()
        manifest.write_text("image_id,s3_url\nalpha,https://images.example.com/alpha\n")
        plan = r.load_plan(self.root)
        self.assertEqual(plan["rows"][0]["key"], "dataset/images/alpha")
        self.assertEqual(r.image_type(self.data), "image/png")
        row = r.transfer_image(self.store, plan, plan["rows"][0], self.state)
        self.assertEqual(row["status"], "copied_verified")
        self.assertEqual(self.store.objects["dataset/images/alpha"], self.data)
        self.assertEqual(
            r.transfer_image(self.store, plan, plan["rows"][0], self.state)["status"],
            "existing_verified",
        )

    def test_duplicate_ids_and_unsafe_source_urls_stop_plan(self):
        manifest, _tags = self.fixture()
        for rows in (
            "alpha,https://images.example.com/a.png\nalpha,https://images.example.com/b.png",
            "../escape,https://images.example.com/a.png",
            "alpha,https://unapproved.example/a.png",
        ):
            manifest.write_text("image_id,s3_url\n" + rows + "\n")
            with self.assertRaises(ValueError):
                r.load_plan(self.root)


if __name__ == "__main__":
    unittest.main()
