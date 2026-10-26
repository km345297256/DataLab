"""Regression guard for the intentionally reduced AI-DataSeek API surface."""

from app.interfaces.api.routes import router


ROUTE_PATHS = {route.path for route in router.routes}


def test_exploration_library_and_plugin_routes_remain_available():
    expected_paths = {
        "/sessions",
        "/data-library",
        "/data-connectors",
        "/auth/login",
        "/auth/register",
        "/auth/registrations",
        "/skills",
        "/mcp/servers",
        "/renderers",
        "/admin/resource-usage",
        "/admin/tasks",
        "/admin/mcp/servers",
        "/admin/skills",
    }

    assert expected_paths <= ROUTE_PATHS


def test_removed_product_domains_are_not_routable():
    removed_prefixes = (
        "/datasets",
        "/a2a",
        "/browser-connections",
        "/claw",
        "/knowledge-bases",
        "/openai",
        "/scientific-sites",
        "/admin/a2a",
        "/admin/audit",
        "/admin/datasets",
        "/admin/execution-nodes",
        "/admin/knowledge-bases",
        "/admin/models",
        "/admin/safety-rules",
        "/admin/sites",
        "/admin/subagents",
        "/admin/token-quotas",
        "/admin/users",
        "/admin/workspaces",
        "/api-keys",
    )

    assert not any(
        path.startswith(prefix)
        for path in ROUTE_PATHS
        for prefix in removed_prefixes
    )


def test_removed_institution_product_mutations_are_not_routable():
    assert not any("/datasets/" in path or "/data-product-draft" in path for path in ROUTE_PATHS)
