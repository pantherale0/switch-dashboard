import os
import sys
import shutil
import tempfile
import atexit
import pytest

# Ensure TESTING environment flag is set immediately
os.environ["TESTING"] = "1"
os.environ["AUTH_DISABLED"] = "1"

# Create a global isolated temporary directory for test execution
_TEST_DATA_DIR = tempfile.mkdtemp(prefix="switch_dashboard_pytest_")
os.environ["DASHBOARD_DATA_DIR"] = _TEST_DATA_DIR

# Import switch_dashboard.config and immediately bind to isolated directory
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


@pytest.fixture(autouse=True)
def isolate_test_data():
    """Ensure every test has its data directory set to the isolated directory."""
    set_data_dir(_TEST_DATA_DIR)
    yield _TEST_DATA_DIR
    set_data_dir(_TEST_DATA_DIR)
