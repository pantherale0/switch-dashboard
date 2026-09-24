import os
import sys
import shutil
import tempfile
import atexit

# Ensure TESTING environment flag is set immediately
os.environ["TESTING"] = "1"

# Create a global isolated temporary directory for test execution
_TEST_DATA_DIR = tempfile.mkdtemp(prefix="switch_dashboard_unittest_")
os.environ["DASHBOARD_DATA_DIR"] = _TEST_DATA_DIR

from switch_dashboard.config import set_data_dir, PROJECT_ROOT, get_default_config, save_config

set_data_dir(_TEST_DATA_DIR)

# Copy reference assets to isolated directory if they exist in PROJECT_ROOT
for asset in ["device_types.yaml", "mac_vendors.txt", "oui.txt", "oui36.txt"]:
    src = os.path.join(PROJECT_ROOT, asset)
    if os.path.exists(src):
        shutil.copy2(src, os.path.join(_TEST_DATA_DIR, asset))

# Initialize with clean default test config
save_config(get_default_config())


def _cleanup():
    shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True)


atexit.register(_cleanup)
