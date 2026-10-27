import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


import pytest  # noqa: E402
from helpers import make_files  # noqa: E402


@pytest.fixture
def archive(tmp_path):
    """2019/Holiday (2), 2019 (1), 2020 (1) and the root itself (1) = 5 photos."""
    root = tmp_path / "Ayşe"  # a person's name: the non-ASCII path is intentional
    make_files(root, "2019/Holiday/IMG_0001.jpg", "2019/Holiday/IMG_0002.jpg", "2019/IMG_0001.jpg",
               "2020/IMG_0001.jpg", "cover.jpg")
    return str(root)
