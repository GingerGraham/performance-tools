"""tz_name in the manifest of collect-os-metrics.sh and psar.sh (F12)."""

import os
import re
import subprocess
import unittest

OS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "os")


def function_text(script, name):
    with open(os.path.join(OS_DIR, script)) as handle:
        text = handle.read()
    match = re.search(r"^%s\(\) \{\n.*?^\}\n" % name, text, re.S | re.M)
    if not match:
        raise AssertionError("%s not found in %s" % (name, script))
    return match.group(0)


class TzNameTests(unittest.TestCase):
    def detect(self, script, tz):
        env = dict(os.environ)
        env.pop("TZ", None)
        if tz is not None:
            env["TZ"] = tz
        code = function_text(script, "detect_tz_name") + "\ndetect_tz_name\n"
        return subprocess.run(["bash", "-c", code], capture_output=True, text=True, env=env).stdout.strip()

    def test_tz_variable_wins(self):
        for script in ("collect-os-metrics.sh", "psar.sh"):
            self.assertEqual(self.detect(script, "Europe/London"), "Europe/London")

    def test_posix_style_tz_with_leading_colon_is_not_used_as_a_name(self):
        for script in ("collect-os-metrics.sh", "psar.sh"):
            self.assertNotEqual(self.detect(script, ":Europe/Paris"), ":Europe/Paris")

    def test_manifest_writes_the_key_after_timezone(self):
        for script in ("collect-os-metrics.sh", "psar.sh"):
            with open(os.path.join(OS_DIR, script)) as handle:
                text = handle.read()
            self.assertIn('echo "timezone=$(date +\'%Z %z\')"\n        echo "tz_name=$(detect_tz_name)"', text)


if __name__ == "__main__":
    unittest.main()
