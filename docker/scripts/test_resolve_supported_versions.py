import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from resolve_supported_versions import main


class ResolveSupportedVersionsTest(unittest.TestCase):
    def run_script(
        self,
        verilator_tags,
        rtl_buddy_tags,
        manual_verilator=None,
        manual_rtl_buddy=None,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            verilator_file = tmpdir / "verilator.txt"
            rtl_buddy_file = tmpdir / "rtl_buddy.txt"
            output_file = tmpdir / "output.txt"
            verilator_file.write_text("\n".join(verilator_tags) + "\n", encoding="utf-8")
            rtl_buddy_file.write_text("\n".join(rtl_buddy_tags) + "\n", encoding="utf-8")

            argv = [
                "resolve_supported_versions.py",
                "--verilator-tags-file",
                str(verilator_file),
                "--rtl-buddy-tags-file",
                str(rtl_buddy_file),
                "--github-output",
                str(output_file),
            ]
            if manual_verilator is not None:
                argv.extend(["--manual-verilator", manual_verilator])
            if manual_rtl_buddy is not None:
                argv.extend(["--manual-rtl-buddy", manual_rtl_buddy])

            with mock.patch("sys.argv", argv):
                rc = main()
            self.assertEqual(rc, 0)

            values = {}
            for line in output_file.read_text(encoding="utf-8").splitlines():
                key, value = line.split("=", 1)
                values[key] = value
            return values

    def test_selects_latest_three_even_verilator_versions(self):
        values = self.run_script(
            ["v5.041", "v5.042", "v5.044", "v5.046", "v5.048"],
            ["v1.0.0", "v2.0.0"],
        )
        self.assertEqual(
            json.loads(values["verilator_versions"]),
            ["v5.044", "v5.046", "v5.048"],
        )
        self.assertEqual(values["latest_verilator"], "v5.048")

    def test_selects_five_per_major_for_latest_two_rtl_buddy_majors(self):
        values = self.run_script(
            ["v5.042", "v5.044", "v5.046"],
            [
                "v1.0.0",
                "v1.0.1",
                "v2.0.0",
                "v2.0.1",
                "v2.0.2",
                "v2.0.3",
                "v2.0.4",
                "v2.0.5",
                "v3.1.0",
                "v3.1.1",
                "v3.1.2",
                "v3.1.3",
                "v3.1.4",
                "v3.1.5",
                "v3.1.6",
                "v3.1.7-rc1",
            ],
        )
        self.assertEqual(
            json.loads(values["rtl_buddy_versions"]),
            ["v2.0.1", "v2.0.2", "v2.0.3", "v2.0.4", "v2.0.5",
             "v3.1.2", "v3.1.3", "v3.1.4", "v3.1.5", "v3.1.6"],
        )
        self.assertEqual(values["latest_rtl_buddy"], "v3.1.6")

    def test_manual_overrides_are_normalized_and_sorted(self):
        values = self.run_script(
            ["v5.042"],
            ["v1.0.0"],
            manual_verilator="5.046, v5.042, v5.044",
            manual_rtl_buddy="v2.0.1,2.0.0,v1.9.9",
        )
        self.assertEqual(
            json.loads(values["verilator_versions"]),
            ["v5.042", "v5.044", "v5.046"],
        )
        self.assertEqual(
            json.loads(values["rtl_buddy_versions"]),
            ["v1.9.9", "v2.0.0", "v2.0.1"],
        )
        self.assertEqual(values["latest_verilator"], "v5.046")
        self.assertEqual(values["latest_rtl_buddy"], "v2.0.1")


if __name__ == "__main__":
    unittest.main()
