"""Tests for the generated model-schema documentation."""

from scripts.generate_model_schema_docs import render_installed_schema_docs


def test_generated_schema_docs_cover_both_srcnet_domains():
    markdown = render_installed_schema_docs()

    assert "`srcnet.data_products`" in markdown
    assert "`srcnet.software`" in markdown
    assert "`srcnet.software_artifacts`" in markdown
    assert "`software.data_products`" not in markdown
    assert "| `product_id` |" in markdown
    assert "| `uri` |" in markdown
    assert "resources.min_memory" in markdown
    assert "generated from the installed pydantic data models" in markdown
