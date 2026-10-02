"""Конфигурация Sphinx для документации на исходный код СПДО."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import spdo  # noqa: E402  путь к исходникам добавлен строкой выше

project = "СПДО"
author = "В.А. Елькин, Д.С. Красавин"
copyright = "2026, " + author
release = spdo.__version__
language = "ru"

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.autosummary",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "sphinx.ext.inheritance_diagram",
    "sphinx.ext.graphviz",
]

autosummary_generate = True
autodoc_default_options = {
    "members": True,
    "undoc-members": False,
    "show-inheritance": True,
    "member-order": "bysource",
}
autodoc_typehints = "description"
autodoc_mock_imports = ["sentence_transformers", "sklearn", "rank_bm25", "pgvector", "psycopg"]

napoleon_google_docstring = True
napoleon_numpy_docstring = False
napoleon_use_ivar = True

graphviz_output_format = "svg"
inheritance_graph_attrs = {"rankdir": "TB"}

html_theme = "furo"
html_title = "СПДО — документация на код"
templates_path = ["_templates"]
exclude_patterns = ["_build"]
