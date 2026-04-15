import json
import os
import shutil
import unittest
import uuid

import projects_context_manager as pcm


class TestRequestValueToBool(unittest.TestCase):
    def test_truthy_values(self):
        truthy_values = [True, 1, "1", "true", "TRUE", "t", "yes", "Y", "on"]
        for value in truthy_values:
            with self.subTest(value=value):
                self.assertTrue(pcm._request_value_to_bool(value))

    def test_falsey_values(self):
        falsey_values = [False, 0, 0.0, None, "", "0", "false", "FALSE", "no", "off", "random"]
        for value in falsey_values:
            with self.subTest(value=value):
                self.assertFalse(pcm._request_value_to_bool(value))


class TestUpsertProject(unittest.TestCase):
    def setUp(self):
        base_tmp = os.path.join(os.getcwd(), "tests", ".tmp")
        os.makedirs(base_tmp, exist_ok=True)
        self.tmpdir = os.path.join(base_tmp, f"pcm-test-{uuid.uuid4().hex}")
        os.makedirs(self.tmpdir, exist_ok=True)
        self.projects_file = os.path.join(self.tmpdir, "tracked_projects.csv")
        with open(self.projects_file, "w", encoding="utf-8") as f:
            json.dump({}, f)

        self.original_projects_dict = pcm.projects_dict
        self.original_projects_dict_filepath = pcm.projects_dict_filepath

        pcm.projects_dict = {}
        pcm.projects_dict_filepath = self.projects_file

    def tearDown(self):
        pcm.projects_dict = self.original_projects_dict
        pcm.projects_dict_filepath = self.original_projects_dict_filepath
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _read_disk_projects(self):
        with open(self.projects_file, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_upsert_inserts_new_project_and_persists(self):
        project = {
            "id": "abc123",
            "project_name": "demo",
            "project_root_path": "/tmp/demo",
            "last_command_timestamp": 1.0,
        }

        changed = pcm.upsert_project(project)
        self.assertTrue(changed)

        disk = self._read_disk_projects()
        self.assertIn("abc123", disk)
        self.assertEqual("demo", disk["abc123"]["project_name"])

    def test_upsert_no_change_returns_false(self):
        project = {
            "id": "abc123",
            "project_name": "demo",
            "project_root_path": "/tmp/demo",
            "last_command_timestamp": 1.0,
        }
        self.assertTrue(pcm.upsert_project(project))

        changed = pcm.upsert_project(project)
        self.assertFalse(changed)

    def test_upsert_updates_existing_metadata_and_persists(self):
        project = {
            "id": "abc123",
            "project_name": "demo",
            "project_root_path": "/tmp/demo",
            "last_command_timestamp": 1.0,
        }
        self.assertTrue(pcm.upsert_project(project))

        updated = dict(project)
        updated["last_command_timestamp"] = 2.0
        updated["runtime_dir_path"] = "/tmp/demo/.remote-xcode-server"

        changed = pcm.upsert_project(updated)
        self.assertTrue(changed)

        disk = self._read_disk_projects()
        self.assertEqual(2.0, disk["abc123"]["last_command_timestamp"])
        self.assertEqual("/tmp/demo/.remote-xcode-server", disk["abc123"]["runtime_dir_path"])


if __name__ == "__main__":
    unittest.main()
