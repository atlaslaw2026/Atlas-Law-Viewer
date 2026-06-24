import importlib
from pathlib import Path


def test_expected_project_files_exist():
    required = [
        "atlas_law_server.py",
        "start_atlas_server.ps1",
        "requirements.txt",
        "README.md",
    ]
    for rel_path in required:
        assert Path(rel_path).exists(), f"Missing required file: {rel_path}"


def test_core_modules_importable():
    modules = [
        "atlas_law_server",
        "atlas_law_v1",
        "atlas_law_viewer",
        "central_district_viewer",
        "supreme_court_viewer",
    ]
    for module_name in modules:
        imported = importlib.import_module(module_name)
        assert imported is not None
